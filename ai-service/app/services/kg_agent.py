"""
知识图谱问答 Agent（DeepAgents 实现，#72 增强）
模型：DeepSeek（SiliconFlow OpenAI 兼容端点）

编排结构（多 Agent）：
- 主 Agent（orchestrator）：理解问题、决定直接检索还是派发子 Agent、汇总回答
  - detail-researcher（子 Agent）：local_search + segment_search，深挖具体实体/关系/原文
  - theme-analyst（子 Agent）：global_search（map-reduce 版），主题/概括类分析
- QuickJS eval 工具（WASM 沙箱，默认零权限）：检索结果的计算/统计/数据变换
- 上下文压缩：create_deep_agent 默认栈自带 SummarizationMiddleware（超阈值自动摘要）
- 会话历史：调用方（Java）从 MySQL 加载最近 N 条传入，跨轮记忆
"""
import time
from typing import Dict, List, Optional
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI
from app.core.config import settings
from app.core.logging import logger
from app.services.graph_retriever import GraphRetriever

# QuickJS 解释器（可选依赖：langchain-quickjs；装不上则降级无 eval 工具）
try:
    from langchain_quickjs import CodeInterpreterMiddleware
    _QUICKJS_AVAILABLE = True
except ImportError:
    _QUICKJS_AVAILABLE = False

SINGLE_VIDEO_NOTE = """
注意：当前问答范围限定在【单个视频】内。检索工具只返回这个视频的内容。
用户说"这个视频"、"该视频"时就指这个视频；如果检索不到相关信息，说明这个视频里确实没有，如实告知即可。
"""

ORCHESTRATOR_PROMPT = """你是一个视频知识图谱问答的编排者（orchestrator）。用户的问题基于一组已解析的视频内容，存储在知识图谱中。

你可以直接调用四个检索工具：
1. media_search —— 视频级粗筛：按视频整体内容（总结向量）判断"这个问题跟哪几个视频有关"，返回候选视频的标题/内容简介/相关度。适合"哪个视频讲了XX"、"这些视频分别讲了什么"、跨视频对比类问题，也适合作为复杂问题的第一步（先定范围再深挖）。
2. local_search —— 检索具体实体和它们之间的关系。适合事实类问题（XX 是什么、为什么、多少）。
3. global_search —— 检索主题社区摘要（已做问题相关性过滤）。适合概括类问题（整体讲了什么、有哪些主题）。
4. segment_search —— 检索视频片段原文。适合需要原话佐证的问题。

检索策略（重要）：
- 问题明显针对具体事实/实体时，直接 local_search 或 segment_search
- 问题是"哪个视频/这些视频"类、或你不确定答案在哪个视频时，先 media_search 确定候选视频，再对候选范围下钻（local/segment）
- 不要对显而易见的问题滥用 media_search；不要连续多次调用同一工具换汤不换药

对于**复杂的、多视角的**问题（例如既要细节又要概括、需要对比多个主题），用 task 工具派发子 Agent：
- detail-researcher：深挖具体事实与原文证据（实体/关系/片段级）
- theme-analyst：主题与脉络概括（社区级）
可以并行派发多个子 Agent，再把它们的结论汇总成最终答案。

另有一个 eval 工具（QuickJS 沙箱里的 JavaScript）：需要对检索结果做计算、统计、排序、格式化时使用。

行为准则：
- 答案必须严格基于检索/子 Agent 返回的内容，禁止编造
- 检索结果不足时，明确告诉用户"知识库中没有相关信息"，不要强行回答
- 用中文回答，简洁清晰
- 标注来源（#74）：答案中涉及具体事实、数字、原话时，在句末标注来源片段，格式固定为〔媒体X mm:ss〕（X 为检索结果中的媒体 id，mm:ss 为该片段时间范围内的时点）。只标注检索结果里真实出现过的媒体和时间，禁止编造；概括性陈述不需要标注
"""

DETAIL_RESEARCHER_PROMPT = """你是细节研究子 Agent。任务：围绕主 Agent 给你的子问题，用 local_search（实体/关系）和 segment_search（片段原文）深挖事实细节。
- 可以多次检索、换角度检索
- 返回：结构化的调查发现（实体、关系、关键原文），标注信息来自哪次检索
- 严格基于检索结果，禁止编造；查不到就明说
"""

THEME_ANALYST_PROMPT = """你是主题分析子 Agent。任务：围绕主 Agent 给你的子问题，用 global_search 分析主题与脉络（社区摘要已按问题做过要点过滤）。
- 可以换措辞多次检索
- 返回：主题层面的概括结论 + 支撑要点
- 严格基于检索结果，禁止编造；查不到就明说
"""


class KGAgent:
    def __init__(self):
        self.retriever = GraphRetriever()
        # 2026-09-02 修复：原来硬编码 siliconflow_*，供应商切换后问答全 402。
        # 改走 runtime_config（Redis 覆盖 > .env），与其他客户端一致。
        from app.core.runtime_config import runtime_config
        cfg = runtime_config.get('llm')
        self.model = ChatOpenAI(
            model=cfg['model'],
            api_key=cfg['api_key'],
            base_url=cfg['base_url'],
            temperature=0,
            timeout=120,
        )

    # ==================== 入口 ====================

    async def ask(self, question: str, group_id: str, media_id: int = None,
                  history: List[Dict] = None) -> Dict:
        """
        Agent 编排问答（group 内闭包：只能检索到本用户的图谱）
        history: 会话历史 [{"role": "user"/"assistant", "content": ...}]，按时间序
        返回 {answer, citations, tool_calls}
        """
        start = time.time()
        citations: List[Dict] = []
        tool_call_log: List[str] = []

        agent = self._build_agent(citations, tool_call_log, group_id, media_id)
        messages = self._build_messages(question, history)

        logger.info(f"[KG_AGENT] Question: {question} (media_id={media_id}, history={len(history or [])})")
        result = await agent.ainvoke({"messages": messages})

        answer = result['messages'][-1].content
        if isinstance(answer, list):
            answer = ''.join(b.get('text', '') for b in answer if isinstance(b, dict))

        duration = (time.time() - start) * 1000
        logger.info(f"[KG_AGENT] Answered in {duration:.2f}ms, tool calls: {tool_call_log}")

        return {
            'answer': answer,
            'citations': self._dedup_citations(citations),
            'tool_calls': tool_call_log
        }

    async def ask_stream(self, question: str, group_id: str, media_id: int = None,
                         history: List[Dict] = None):
        """
        流式问答：yield 思考过程事件（group 内闭包，media_id 可选限定单视频）
        事件类型: tool_start / tool_end / subagent_start / subagent_end / final
        """
        citations: List[Dict] = []
        tool_call_log: List[str] = []

        agent = self._build_agent(citations, tool_call_log, group_id, media_id)
        messages = self._build_messages(question, history)

        logger.info(f"[KG_AGENT] Stream question: {question} (media_id={media_id}, history={len(history or [])})")

        our_tools = {'local_search', 'global_search', 'segment_search', 'eval'}
        final_answer = None

        async for event in agent.astream_events({"messages": messages}, version="v2"):
            kind = event.get('event')
            name = event.get('name', '')

            if kind == 'on_tool_start' and name in our_tools:
                inputs = event.get('data', {}).get('input') or {}
                query = inputs.get('query') or inputs.get('code') or ''
                yield {'type': 'tool_start', 'tool': name, 'query': str(query)[:300]}

            elif kind == 'on_tool_start' and name == 'task':
                # 子 Agent 派发（多 Agent 编排事件）
                inputs = event.get('data', {}).get('input') or {}
                subagent = inputs.get('subagent_type', '') if isinstance(inputs, dict) else ''
                desc = inputs.get('description', '') if isinstance(inputs, dict) else ''
                tool_call_log.append(f"task({subagent}: {desc})")
                yield {'type': 'subagent_start', 'subagent': subagent, 'task': str(desc)[:200]}

            elif kind == 'on_tool_end' and name == 'task':
                output = event.get('data', {}).get('output', '')
                preview = getattr(output, 'content', str(output))
                if isinstance(preview, list):
                    preview = ''.join(b.get('text', '') for b in preview if isinstance(b, dict))
                yield {'type': 'subagent_end', 'result_preview': str(preview)[:600]}

            elif kind == 'on_tool_end' and name in our_tools:
                output = event.get('data', {}).get('output', '')
                preview = getattr(output, 'content', str(output))
                if isinstance(preview, list):
                    preview = ''.join(b.get('text', '') for b in preview if isinstance(b, dict))
                yield {'type': 'tool_end', 'tool': name, 'result_preview': str(preview)[:600]}

            elif kind == 'on_chain_end':
                # 根节点的最终输出里带完整 messages
                output = event.get('data', {}).get('output')
                if isinstance(output, dict) and output.get('messages'):
                    content = getattr(output['messages'][-1], 'content', None)
                    if isinstance(content, list):
                        content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
                    if content:
                        final_answer = content

        yield {
            'type': 'final',
            'answer': final_answer or "（未能获取最终答案）",
            'citations': self._dedup_citations(citations),
            'tool_calls': tool_call_log
        }

    # ==================== 组装 ====================

    def _build_agent(self, citations: List[Dict], tool_call_log: List[str], group_id: str,
                     media_id: int = None):
        """组装 deep agent：三检索工具 + 子 Agent 编排 + QuickJS eval + 内置上下文压缩"""
        tools = self._build_tools(citations, tool_call_log, group_id, media_id)
        local_tool, global_tool, segment_tool, media_tool = tools

        subagents = [
            {
                "name": "detail-researcher",
                "description": "深挖具体事实与原文证据：实体定义、实体间关系、片段原话。适合事实型子问题。",
                "system_prompt": DETAIL_RESEARCHER_PROMPT,
                "tools": [local_tool, segment_tool],
            },
            {
                "name": "theme-analyst",
                "description": "主题与脉络概括：这些视频整体讲了什么、某个主题的全貌。适合概括型子问题。",
                "system_prompt": THEME_ANALYST_PROMPT,
                "tools": [global_tool],
            },
        ]

        middleware = []
        if _QUICKJS_AVAILABLE:
            # QuickJS WASM 沙箱：默认零权限（无文件/网络/shell），不开 PTC（安全默认）
            middleware.append(CodeInterpreterMiddleware())

        return create_deep_agent(
            model=self.model,
            tools=tools,
            system_prompt=ORCHESTRATOR_PROMPT + (SINGLE_VIDEO_NOTE if media_id else ""),
            subagents=subagents,
            middleware=middleware,
        )

    @staticmethod
    def _build_messages(question: str, history: Optional[List[Dict]]) -> List[Dict]:
        """会话历史 + 当前问题 → agent messages（只保留 user/assistant 文本轮次）"""
        messages = []
        for h in (history or []):
            role = h.get('role')
            content = h.get('content')
            if role in ('user', 'assistant') and content:
                messages.append({"role": role, "content": str(content)})
        messages.append({"role": "user", "content": question})
        return messages

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
            """检索知识图谱中的主题社区要点（map-reduce 过滤后）。适合概括类问题，如"这些视频讲了什么"、"关于XX的整体情况"。返回与问题相关的主题要点。"""
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

        async def media_search(query: str) -> str:
            """视频级粗筛：按视频整体内容判断"这个问题跟哪几个视频有关"。适合"哪个视频讲了XX"/"这些视频分别讲了什么"/跨视频对比，或作为复杂问题确定下钻范围的第一步。返回候选视频标题、内容简介和相关度。"""
            tool_call_log.append(f"media_search({query})")
            result = await self.retriever.media_search(query, group_id)
            return "\n".join(result['contexts']) if result['contexts'] else "没有相关视频"

        return [local_search, global_search, segment_search, media_search]

    def _dedup_citations(self, citations: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for c in citations:
            if c['segment_id'] not in seen:
                seen.add(c['segment_id'])
                unique.append(c)
        return unique
