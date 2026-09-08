"""
完整视频处理 Pipeline 接口
"""
import asyncio
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.video_processing_pipeline import VideoProcessingPipeline
from app.core.logging import logger, log_request, log_response, log_performance

router = APIRouter()


class ProcessVideoRequest(BaseModel):
    video_path: str
    media_id: int
    user_goal: str = ""
    force: bool = False  # True 时忽略已有处理结果，强制重跑解析和抽取
    attempt: int = 1     # 重投次数（Java watcher 失败重投时递增，仅记录用）
    user_id: int = 0     # 用户隔离（三期），图谱数据落 group_id=user_{user_id}
    upload_time_ms: int = None  # #84 视频真实上传时间（毫秒戳），写入 Media.uploaded_at


class ProcessVideoResponse(BaseModel):
    status: str
    media_id: int
    statistics: dict
    duration_ms: float


@router.post("/build-async", status_code=202)
async def build_async(request: ProcessVideoRequest):
    """
    异步构建（旧链路兼容，内部仍走 analyze+commit 一体 runner）
    新代码请用 analyze-async / commit-async（#66 两阶段拆分）
    """
    from app.services import kg_task_runner

    logger.info(f"[API] build-async: media_id={request.media_id} path={request.video_path} attempt={request.attempt}")
    result = await kg_task_runner.start_build(request.media_id, request.video_path,
                                              attempt=request.attempt, user_id=request.user_id)
    if result == kg_task_runner.BUSY:
        raise HTTPException(status_code=429, detail="构建任务并发已达上限，请稍后重试")
    return {
        'status': 'accepted',
        'media_id': request.media_id,
        'queued': result == kg_task_runner.QUEUED,  # False 表示重复投递被幂等吞掉
    }


@router.post("/analyze-async", status_code=202)
async def analyze_async(request: ProcessVideoRequest):
    """
    analyze 阶段异步投递（#66）：解析+抽取后台执行，完成后回调 Java /internal/kg/analyzed
    - 重复投递幂等：queued=false
    - 并发上限背压：429，MQ 消费端据此延迟重投
    """
    from app.services import kg_task_runner

    logger.info(f"[API] analyze-async: media_id={request.media_id} attempt={request.attempt} upload_time_ms={request.upload_time_ms}")
    result = await kg_task_runner.start_analyze(request.media_id, request.video_path,
                                                attempt=request.attempt, user_id=request.user_id,
                                                upload_time_ms=request.upload_time_ms)
    if result == kg_task_runner.BUSY:
        raise HTTPException(status_code=429, detail="analyze 并发已达上限，请稍后重试")
    return {
        'status': 'accepted',
        'media_id': request.media_id,
        'queued': result == kg_task_runner.QUEUED,
    }


class CommitAsyncRequest(BaseModel):
    media_id: int
    attempt: int = 1
    user_id: int = 0


@router.post("/commit-async", status_code=202)
async def commit_async(request: CommitAsyncRequest):
    """
    commit 阶段异步投递（#66）：自持 per-user 写锁执行图写入，完成后回调 Java /internal/kg/finished
    """
    from app.services import kg_task_runner

    logger.info(f"[API] commit-async: media_id={request.media_id} attempt={request.attempt}")
    result = await kg_task_runner.start_commit(request.media_id,
                                               attempt=request.attempt, user_id=request.user_id)
    if result == kg_task_runner.BUSY:
        raise HTTPException(status_code=429, detail="commit 并发已达上限，请稍后重试")
    return {
        'status': 'accepted',
        'media_id': request.media_id,
        'queued': result == kg_task_runner.QUEUED,
    }


@router.delete("/media/{media_id}")
async def delete_media_graph(media_id: int, phase: int = 0, user_id: int = 0):
    """
    删除视频的图谱数据（Java 删除视频时调用）
    锁在 Python 自持（per-user：kg:graph:write:{group}），与同 group 的 commit 互斥
    phase=1: 快速段——只删 Media + Segment（用户视角立即删除）
    phase=2: 清理段——关系过滤、孤儿实体、空社区、source_count 重算（全部限定本 group，
             防止误删其他用户 commit 中途"还没挂 MENTIONED_IN"的暂态孤儿实体）
    phase=0: 全量（兼容旧调用）
    """
    from app.core.kglock import KgLock

    group_id = f"user_{user_id}"
    try:
        async with KgLock(f"kg:graph:write:{group_id}"):
            # 阻塞 Neo4j 调用丢线程池，持锁期间不冻结事件循环
            return await asyncio.to_thread(_do_delete_media_graph, media_id, phase, group_id)
    except TimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))


def _do_delete_media_graph(media_id: int, phase: int, group_id: str):
    from app.clients.neo4j_client import Neo4jClient
    client = Neo4jClient()
    prefix = f"media_{media_id}_"

    try:
        with client.driver.session() as s:
            stats = {}

            if phase in (0, 1):
                # 快速段：片段 + Media（连带 MENTIONED_IN / HAS_SEGMENT）
                stats['segments'] = s.run("""
                    MATCH (s:Segment {media_id: $mid}) DETACH DELETE s
                """, mid=media_id).consume().counters.nodes_deleted
                stats['media'] = s.run("""
                    MATCH (m:Media {id: $id}) DETACH DELETE m
                """, id=f"media_{media_id}").consume().counters.nodes_deleted

            if phase in (0, 2):
                # 清理段
                stats['relationships'] = s.run("""
                    MATCH ()-[r:RELATES_TO]->()
                    WHERE r.source_segment_ids IS NOT NULL
                    SET r.source_segment_ids = [sid IN r.source_segment_ids WHERE NOT sid STARTS WITH $prefix]
                    WITH r WHERE size(r.source_segment_ids) = 0
                    DELETE r
                """, prefix=prefix).consume().counters.relationships_deleted

                r = s.run("""
                    MATCH (e:Entity {group_id: $gid}) WHERE NOT (e)-[:MENTIONED_IN]->()
                    DETACH DELETE e
                """, gid=group_id)
                stats['orphan_entities'] = r.consume().counters.nodes_deleted

                stats['empty_communities'] = s.run("""
                    MATCH (c:Community {group_id: $gid}) WHERE NOT (:Entity)-[:BELONGS_TO]->(c)
                    DETACH DELETE c
                """, gid=group_id).consume().counters.nodes_deleted
                s.run("""
                    MATCH (c:Community {group_id: $gid})
                    OPTIONAL MATCH (e:Entity)-[:BELONGS_TO]->(c)
                    WITH c, count(e) as cnt
                    SET c.entity_count = cnt
                """, gid=group_id)
                s.run("""
                    MATCH (e:Entity {group_id: $gid})
                    OPTIONAL MATCH (e)-[m:MENTIONED_IN]->()
                    WITH e, count(m) as cnt SET e.source_count = cnt
                """, gid=group_id)
                s.run("""
                    MATCH ()-[r:RELATES_TO {group_id: $gid}]->() WHERE r.source_segment_ids IS NOT NULL
                    SET r.source_count = size(r.source_segment_ids)
                """, gid=group_id)

        logger.info(f"[PIPELINE] Deleted graph data for media {media_id} (phase={phase}): {stats}")
        return {'status': 'success', 'media_id': media_id, 'phase': phase, 'deleted': stats}
    except Exception as e:
        logger.error(f"Failed to delete media graph: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


class CommitRequest(BaseModel):
    media_id: int
    user_id: int = 0


@router.post("/analyze")
async def analyze_video(request: ProcessVideoRequest):
    """
    阶段一：解析 + 抽取（无锁并发，只写视频私有数据，天然幂等）
    """
    logger.info(f"[API] analyze: media_id={request.media_id} path={request.video_path}")
    try:
        pipeline = VideoProcessingPipeline()
        return await pipeline.analyze(request.video_path, request.media_id, request.user_goal, request.force,
                                      group_id=f"user_{request.user_id}")
    except Exception as e:
        logger.error(f"Failed to analyze video: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/commit")
async def commit_graph(request: CommitRequest):
    """
    阶段二：图写入（消歧→去重→入库→社区增量）
    锁在 Python 自持（per-user：kg:graph:write:{group}），调用方无需再持锁
    """
    from app.core.kglock import KgLock

    group_id = f"user_{request.user_id}"
    logger.info(f"[API] commit: media_id={request.media_id} group={group_id}")
    try:
        async with KgLock(f"kg:graph:write:{group_id}"):
            pipeline = VideoProcessingPipeline()
            return await pipeline.commit(request.media_id, group_id=group_id)
    except TimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to commit graph: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/process", response_model=ProcessVideoResponse)
async def process_video(request: ProcessVideoRequest):
    """
    完整视频处理 Pipeline（analyze+commit 组合，无锁，仅单机测试用）
    """
    start_time = time.time()

    log_request("/api/v1/pipeline/process", {
        "video_path": request.video_path,
        "media_id": request.media_id,
        "user_goal": request.user_goal
    })

    try:
        pipeline = VideoProcessingPipeline()
        result = await pipeline.process(request.video_path, request.media_id, request.user_goal, request.force)

        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/pipeline/process", 200, result['statistics'])
        log_performance("/api/v1/pipeline/process", duration_ms, result['statistics'])

        return ProcessVideoResponse(**result)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/pipeline/process", 500, error=str(e))
        log_performance("/api/v1/pipeline/process", duration_ms, {"error": str(e)})

        logger.error(f"Failed to process video: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
