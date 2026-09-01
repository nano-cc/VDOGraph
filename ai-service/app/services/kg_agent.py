"""
知识图谱问答 Agent（DeepAgents 实现）
模型：DeepSeek（SiliconFlow OpenAI 兼容端点）
工具：图谱检索层的 local/global/segment 三种检索
"""
import time
from typing import Dict, List
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI
from app.core.config import settings
from app.core.logging import logger
from app.services.graph_retriever import GraphRetriever

SINGLE_VIDEO_NOTE = """
注意：当前问答范围限定在【单个视频】内。检索工具只返回这个视频的内容。
用户说"这个视频"、"该视频"时就指这个视频；如果检索不到相关信息，说明这个视频里确实没有，如实告知即可。
"""

SYSTEM_PROMPT = """你是一个视频知识图谱问答助手。用户的问题基于一组已解析的视频内容，这些信息存储在知识图谱中。

你有三个检索工具：
1. local_search —— 检索具体实体和它们之间的关系。适合事实类问题（XX 是什么、为什么、多少）。
2. global_search —— 检索主题社区摘要。适合概括类问题（整体讲了什么、有哪些主题）。
3. segment_search —— 检索视频片段原文。适合需要原话佐证的问题。

行为准则：
- 根据问题类型选择合适的工具，一个问题可以多次调用不同工具补充信息
- 答案必须严格基于检索返回的内容，禁止编造检索结果中没有的事实
- 检索结果不足时，明确告诉用户"知识库中没有相关信息"，不要强行回答
- 用中文回答，简洁清晰
"""


class KGAgent:
    def __init__(self):
        self.retriever = GraphRetriever()
        self.model = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            temperature=0,
            timeout=120,
        )

    async def ask(self, question: str, group_id: str, media_id: int = None) -> Dict:
        """
        Agent 自主决策检索并回答（group 内闭包：只能检索到本用户的图谱）
        media_id 可选：限定单个视频范围内问答
        返回 {answer, citations, tool_calls}
        """
        start = time.time()
        citations: List[Dict] = []
        tool_call_log: List[str] = []

        tools = self._build_tools(citations, tool_call_log, group_id, media_id)

        agent = create_deep_agent(
            model=self.model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT + (SINGLE_VIDEO_NOTE if media_id else ""),
        )

        logger.info(f"[KG_AGENT] Question: {question} (media_id={media_id})")
        result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})

        answer = result['messages'][-1].content

        duration = (time.time() - start) * 1000
        logger.info(f"[KG_AGENT] Answered in {duration:.2f}ms, tool calls: {tool_call_log}")

        return {
            'answer': answer,
            'citations': self._dedup_citations(citations),
            'tool_calls': tool_call_log
        }

    async def ask_stream(self, question: str, group_id: str, media_id: int = None):
        """
        流式问答：yield 思考过程事件（group 内闭包，media_id 可选限定单视频）
        事件类型: tool_start / tool_end / final
        """
        citations: List[Dict] = []
        tool_call_log: List[str] = []

        tools = self._build_tools(citations, tool_call_log, group_id, media_id)

        agent = create_deep_agent(
            model=self.model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT + (SINGLE_VIDEO_NOTE if media_id else ""),
        )

        logger.info(f"[KG_AGENT] Stream question: {question} (media_id={media_id})")

        our_tools = {'local_search', 'global_search', 'segment_search'}
        final_answer = None

        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": question}]},
            version="v2"
        ):
            kind = event.get('event')
            name = event.get('name', '')

            if kind == 'on_tool_start' and name in our_tools:
                query = (event.get('data', {}).get('input') or {}).get('query', '')
                yield {'type': 'tool_start', 'tool': name, 'query': query}

            elif kind == 'on_tool_end' and name in our_tools:
                output = event.get('data', {}).get('output', '')
                # ToolMessage 对象的 content 是文本
                preview = getattr(output, 'content', str(output))
                yield {'type': 'tool_end', 'tool': name, 'result_preview': preview[:600]}

            elif kind == 'on_chain_end':
                # 根节点的最终输出里带完整 messages
                output = event.get('data', {}).get('output')
                if isinstance(output, dict) and output.get('messages'):
                    content = getattr(output['messages'][-1], 'content', None)
                    if isinstance(content, list):
                        # 兼容 content blocks 形式
                        content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
                    if content:
                        final_answer = content

        yield {
            'type': 'final',
            'answer': final_answer or "（未能获取最终答案）",
            'citations': self._dedup_citations(citations),
            'tool_calls': tool_call_log
        }

    def _build_tools(self, citations: List[Dict], tool_call_log: List[str], group_id: str,
                     media_id: int = None):
        """构建三个检索工具（citations/tool_call_log 通过闭包收集）"""

        async def local_search(query: str) -> str:
            """检索知识图谱中的具体实体及其关系。适合事实类问题，如"XX是什么"、"为什么XX"、"XX是多少"。返回实体描述和实体间关系。"""
            tool_call_log.append(f"local_search({query})")
            result = await self.retriever.local_search(query, group_id, media_id=media_id)
            citations.extend(result['citations'])
            return "\n".join(result['contexts']) if result['contexts'] else "未检索到相关内容"

        async def global_search(query: str) -> str:
            """检索知识图谱中的主题社区摘要。适合概括类问题，如"这些视频讲了什么"、"关于XX的整体情况"。返回主题摘要。"""
            tool_call_log.append(f"global_search({query})")
            result = await self.retriever.global_search(query, group_id, media_id=media_id)
            citations.extend(result['citations'])
            return "\n".join(result['contexts']) if result['contexts'] else "未检索到相关内容"

        async def segment_search(query: str) -> str:
            """检索视频片段的原文（语音转写文本）。适合需要原话佐证的问题。返回片段原文和时间戳。"""
            tool_call_log.append(f"segment_search({query})")
            result = await self.retriever.segment_search(query, group_id, media_id=media_id)
            citations.extend(result['citations'])
            return "\n".join(result['contexts']) if result['contexts'] else "未检索到相关内容"

        return [local_search, global_search, segment_search]

    def _dedup_citations(self, citations: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for c in citations:
            if c['segment_id'] not in seen:
                seen.add(c['segment_id'])
                unique.append(c)
        return unique
