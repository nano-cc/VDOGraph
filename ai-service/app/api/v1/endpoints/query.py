"""
知识图谱查询接口
"""
import time
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from pydantic import BaseModel

from app.clients.neo4j_client import Neo4jClient
from app.clients.embedding import EmbeddingClient
from app.clients.deepseek import DeepSeekClient
from app.core.logging import logger

router = APIRouter()


class AskRequest(BaseModel):
    question: str
    mode: str = "auto"  # auto=agent 自主决策 / local / global（固定流程）
    user_id: int = 0    # 用户隔离（三期）：检索限定 group_id=user_{user_id}
    media_id: int = None  # 可选：限定单个视频范围内问答（Video Agent 单视频模式）


class AskResponse(BaseModel):
    answer: str
    citations: list
    mode: str
    tool_calls: list = []
    duration_ms: float


@router.post("/ask/stream")
async def ask_stream(request: AskRequest):
    """
    流式问答（SSE）：实时推送 agent 思考过程（工具调用及结果），最后推送答案
    事件格式: data: {"type": "tool_start"|"tool_end"|"final"|"error", ...}
    """
    import json
    from fastapi.responses import StreamingResponse
    from app.services.kg_agent import KGAgent

    async def event_generator():
        agent = KGAgent()
        try:
            async for event in agent.ask_stream(request.question, group_id=f"user_{request.user_id}",
                                                media_id=request.media_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"Stream ask failed: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


DIRECT_ANSWER_PROMPT = """基于以下从视频知识图谱中检索到的内容，回答用户的问题。

检索内容:
{context}

用户问题: {question}

要求:
- 答案必须严格基于检索内容，禁止编造
- 检索内容不足时明确说明"知识库中没有相关信息"
- 用中文简洁回答
"""


@router.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    """
    知识图谱问答
    mode=auto: DeepAgents agent 自主决策检索
    mode=local/global: 固定流程（检索 -> LLM 生成），保底路径
    """
    start = time.time()

    try:
        if request.mode == "auto":
            from app.services.kg_agent import KGAgent
            agent = KGAgent()
            result = await agent.ask(request.question, group_id=f"user_{request.user_id}",
                                     media_id=request.media_id)
            return AskResponse(
                answer=result['answer'],
                citations=result['citations'],
                mode='auto',
                tool_calls=result['tool_calls'],
                duration_ms=(time.time() - start) * 1000
            )

        # 固定流程：local / global
        from app.services.graph_retriever import GraphRetriever
        retriever = GraphRetriever()

        group_id = f"user_{request.user_id}"
        if request.mode == "local":
            retrieval = await retriever.local_search(request.question, group_id)
        elif request.mode == "global":
            retrieval = await retriever.global_search(request.question, group_id)
        else:
            raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")

        context = "\n".join(retrieval['contexts']) if retrieval['contexts'] else "（未检索到相关内容）"
        deepseek = DeepSeekClient()
        answer = deepseek.call_llm(DIRECT_ANSWER_PROMPT.format(context=context, question=request.question))

        return AskResponse(
            answer=answer,
            citations=retrieval['citations'],
            mode=request.mode,
            duration_ms=(time.time() - start) * 1000
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to answer question: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/search")
async def search_knowledge_graph(
    query: str = Query(..., description="搜索关键词"),
    media_id: Optional[int] = Query(None, description="视频 ID（可选）"),
    user_id: int = Query(2, description="用户 ID（调试用，默认历史测试用户）")
):
    """
    搜索知识图谱
    """
    try:
        logger.info(f"[QUERY] Searching knowledge graph: {query}")

        neo4j_client = Neo4jClient()
        embedding_client = EmbeddingClient()

        # 计算查询的 Embedding
        query_embedding = await embedding_client.embed(query)

        # 向量相似度查询（group 内闭包）
        results = neo4j_client.find_similar_entities(query_embedding, f"user_{user_id}", top_k=10, threshold=0.5)

        # 过滤 media_id（如果提供）
        if media_id:
            # TODO: 实现按 media_id 过滤
            pass

        return {
            'entities': [r['entity'] for r in results],
            'scores': [r['score'] for r in results]
        }

    except Exception as e:
        logger.error(f"Failed to search knowledge graph: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entity/{entity_id}")
async def get_entity(entity_id: str, user_id: int = Query(2, description="用户 ID（调试用）")):
    """
    获取实体详情
    """
    try:
        logger.info(f"[QUERY] Getting entity: {entity_id}")

        neo4j_client = Neo4jClient()
        entity = neo4j_client.find_entity_by_name(entity_id, f"user_{user_id}")

        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        return entity

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get entity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entity/{entity_id}/sources")
async def get_entity_sources(entity_id: str):
    """
    获取实体来源（追溯）
    """
    try:
        logger.info(f"[QUERY] Getting entity sources: {entity_id}")

        neo4j_client = Neo4jClient()
        sources = neo4j_client.get_entity_sources(entity_id)

        return {
            'sources': sources
        }

    except Exception as e:
        logger.error(f"Failed to get entity sources: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/media/{media_id}/transcript")
async def get_media_transcript(media_id: int):
    """
    视频全量文字（复用 KG analyze 产物：Neo4j 片段 transcript 按时间拼接，带时间戳）
    返回 404 表示该视频尚未建图，调用方回退老 ASR 流程
    """
    try:
        neo4j_client = Neo4jClient()
        segments = neo4j_client.get_media_segments(media_id)
        if not segments:
            raise HTTPException(status_code=404, detail="该视频尚未完成图谱解析，无可用转写")

        def fmt(ms):
            sec = (ms or 0) // 1000
            return f"{sec // 60:02d}:{sec % 60:02d}"

        lines = []
        for s in segments:
            text = (s.get('transcript') or '').strip()
            if text:
                lines.append(f"[{fmt(s.get('start_ms'))} - {fmt(s.get('end_ms'))}] {text}")
        return {'media_id': media_id, 'transcript': "\n\n".join(lines), 'segments': len(segments)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get media transcript: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/media/{media_id}/communities")
async def get_media_communities(media_id: int):
    """
    获取视频的所有社区
    """
    try:
        logger.info(f"[QUERY] Getting media communities: {media_id}")

        neo4j_client = Neo4jClient()
        communities = neo4j_client.get_media_communities(media_id)

        return {
            'communities': communities
        }

    except Exception as e:
        logger.error(f"Failed to get media communities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
