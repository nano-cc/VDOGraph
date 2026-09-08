"""
知识图谱构建 后台任务执行器（异步化一期）
- Java 投递即返回，构建在 Python 后台 asyncio 任务中执行
- 状态机写 Redis kg:task:{mediaId}（hash）：QUEUED → ANALYZING → COMMITTING → SUCCESS / FAILED
- updated_at 兼作心跳：Java KgTaskWatcher 据此判死（>30min 无更新视为 worker 死亡）
- commit 段自持 kg:graph:write 锁（kglock），锁跟执行方走
"""
import asyncio
import time
from typing import Dict, Optional

from app.core.config import settings
from app.core.kglock import KgLock, _redis
from app.core.logging import logger

TASK_KEY_PREFIX = "kg:task:"
GRAPH_WRITE_LOCK_PREFIX = "kg:graph:write:"
TASK_TTL_S = 7 * 24 * 3600

# 进程内任务注册表：防同进程重复触发（跨进程/跨重启靠 Redis 状态 + 新鲜心跳判断）
_running_tasks: Dict[int, asyncio.Task] = {}

# 心跳协程间隔：长任务中间无进度事件时也要保活，防止被 watcher 误判死亡
HEARTBEAT_INTERVAL_S = 30

# 并发构建上限：瓶颈是 LLM 限流（~2/s），更多并发没有意义，超出返回 BUSY 让 MQ 延迟重投
MAX_CONCURRENT_BUILDS = 3

# Redis 状态心跳新鲜度阈值：进行中的任务心跳比这个新，视为"有人在跑"（覆盖进程重启竞态）
FRESH_HEARTBEAT_S = 120

QUEUED = "QUEUED"
DUPLICATE = "DUPLICATE"
BUSY = "BUSY"


async def _set_task(media_id: int, **fields):
    client = await _redis()
    key = TASK_KEY_PREFIX + str(media_id)
    mapping = {k: str(v) for k, v in fields.items() if v is not None}
    mapping["updated_at"] = str(int(time.time()))
    await client.hset(key, mapping=mapping)
    await client.expire(key, TASK_TTL_S)


# #78 条件写入 Lua：attempt 不匹配（我是旧执行体/僵尸）则拒绝写入并返回 -1。
# key 不存在（TTL 过期）允许重建——Java watcher 的心跳归属校验以 MySQL attempt 为准，可识别。
_COND_SET_LUA = """
local cur = redis.call('HGET', KEYS[1], 'attempt')
if cur and cur ~= ARGV[1] then return -1 end
local n = #ARGV
for i = 4, n, 2 do
    redis.call('HSET', KEYS[1], ARGV[i], ARGV[i+1])
end
redis.call('HSET', KEYS[1], 'attempt', ARGV[1], 'updated_at', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


async def _set_task_if_owner(media_id: int, attempt: int, **fields) -> bool:
    """#78 条件写入：仅当 Redis 中 attempt 归属当前执行体才写入；返回 False=我是僵尸"""
    client = await _redis()
    key = TASK_KEY_PREFIX + str(media_id)
    args = [str(attempt), str(int(time.time())), str(TASK_TTL_S)]
    for k, v in fields.items():
        if v is not None:
            args.extend([k, str(v)])
    r = await client.eval(_COND_SET_LUA, 1, key, *args)
    return r == 1


def _abort_zombie(media_id: int, attempt: int, phase: str):
    """#78 僵尸自我中止：心跳/进度发现 attempt 不匹配，取消自己的执行体任务"""
    logger.warning(f"[TASK] media_id={media_id} zombie detected (attempt={attempt}, phase={phase}), self-aborting")
    registry = _analyze_running if phase == "analyze" else _commit_running
    task = registry.get(media_id)
    if task is not None and not task.done():
        task.cancel()


async def get_task_state(media_id: int) -> Dict[str, str]:
    client = await _redis()
    return await client.hgetall(TASK_KEY_PREFIX + str(media_id))


async def _heartbeat(media_id: int):
    """长任务保活：定期刷 updated_at"""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            await _set_task(media_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"[TASK] heartbeat error media_id={media_id}: {e}")


def is_running(media_id: int) -> bool:
    task = _running_tasks.get(media_id)
    return task is not None and not task.done()


async def start_build(media_id: int, video_url: str, attempt: int = 1, user_id: int = None) -> str:
    """
    投递后台构建任务，返回 QUEUED / DUPLICATE / BUSY。
    - DUPLICATE：同任务运行中（进程内注册表），或 Redis 状态进行中且心跳新鲜（跨重启/并发消息竞态）
    - BUSY：并发构建数达上限（调用方应延迟重试，MQ 场景下重投）
    """
    if is_running(media_id):
        logger.info(f"[TASK] media_id={media_id} already running, skip")
        return DUPLICATE

    # 跨重启/并发消息竞态：注册表是空的但 Redis 显示执行中且心跳新鲜 → 别人在跑
    # 注意 QUEUED 不算：QUEUED 是投递方发消息"之前"写的，此刻可能还没有任何执行体，
    # 把它当"有人在跑"会把真正的首次投递吞掉
    state = await get_task_state(media_id)
    status = state.get("status", "")
    if status in ("ANALYZING", "COMMITTING"):
        try:
            updated_at = int(state.get("updated_at", "0"))
        except ValueError:
            updated_at = 0
        if time.time() - updated_at < FRESH_HEARTBEAT_S:
            logger.info(f"[TASK] media_id={media_id} fresh running state in Redis (status={status}), skip")
            return DUPLICATE

    running_count = sum(1 for t in _running_tasks.values() if not t.done())
    if running_count >= MAX_CONCURRENT_BUILDS:
        logger.info(f"[TASK] media_id={media_id} busy: {running_count} builds in flight")
        return BUSY

    group_id = f"user_{user_id}"
    await _set_task(media_id, status="QUEUED", progress="", message="排队中", attempt=attempt,
                    video_url=video_url, group_id=group_id)
    task = asyncio.create_task(_run_build(media_id, video_url, attempt, group_id))
    _running_tasks[media_id] = task
    task.add_done_callback(lambda t: _running_tasks.pop(media_id, None))
    logger.info(f"[TASK] media_id={media_id} queued (attempt={attempt})")
    return QUEUED


async def _run_build(media_id: int, video_url: str, attempt: int, group_id: str):
    from app.services.video_processing_pipeline import VideoProcessingPipeline

    heartbeat = asyncio.create_task(_heartbeat(media_id))
    try:
        pipeline = VideoProcessingPipeline()

        # 阶段一：analyze（无锁并发，逐片段写进度）
        await _set_task(media_id, status="ANALYZING", message="解析与抽取中")

        async def analyze_progress(done: int, total: int):
            await _set_task(media_id, progress=f"{done}/{total}", message=f"片段抽取 {done}/{total}")

        analyze_result = await pipeline.analyze(video_url, media_id, progress_cb=analyze_progress,
                                                group_id=group_id)
        logger.info(f"[TASK] media_id={media_id} analyze done: {analyze_result.get('statistics')}")

        # 阶段二：commit（自持全局写锁）
        await _set_task(media_id, status="COMMITTING", progress="", message="图谱写入中（等待写锁）")

        async def commit_progress(stage: str):
            await _set_task(media_id, message=f"图谱写入：{stage}")

        # per-user 写锁（三期）：不同用户的 commit 完全并行，同用户内串行
        async with KgLock(GRAPH_WRITE_LOCK_PREFIX + group_id):
            await _set_task(media_id, message="图谱写入中")
            commit_result = await pipeline.commit(media_id, progress_cb=commit_progress,
                                                  group_id=group_id)

        stats = commit_result.get("statistics")
        await _set_task(media_id, status="SUCCESS", progress="", message="知识图谱已就绪",
                        stats=str(stats) if stats else "")
        logger.info(f"[TASK] media_id={media_id} SUCCESS: {stats}")

    except Exception as e:
        logger.error(f"[TASK] media_id={media_id} FAILED (attempt={attempt}): {e}", exc_info=True)
        await _set_task(media_id, status="FAILED", progress="", message=f"构建失败: {e}")
    finally:
        heartbeat.cancel()


# ============================================================
# #66 两阶段拆分：analyze / commit 独立 runner
# 状态推进全部走 Java 回调（MySQL 事实源），Python 只写 Redis 心跳/进度
# ============================================================

_analyze_running: Dict[int, asyncio.Task] = {}
_commit_running: Dict[int, asyncio.Task] = {}

# 并发上限检查-注册的原子锁：start_analyze/start_commit 里 check→register 之间有 await，
# 批量消息同时到达时多个协程会交错通过计数检查（联调实测 4 个 commit 同时 queued，上限 2）。
# asyncio 单线程下只需在检查+占位注册之间不设 await 即可，用占位 Future 同步占坑。
_slot_placeholders: Dict[str, asyncio.Future] = {}


def _reserve_slot(registry: Dict[int, asyncio.Task], media_id: int, phase: str, max_concurrent: int):
    """同步占坑：返回 None 表示成功，否则返回 DUPLICATE/BUSY。调用后必须尽快用真实 Task 替换占位"""
    existing = registry.get(media_id)
    if existing is not None and not existing.done():
        return DUPLICATE
    if (phase + str(media_id)) in _slot_placeholders:
        return DUPLICATE
    in_flight = sum(1 for t in registry.values() if not t.done()) + \
        sum(1 for k in _slot_placeholders if k.startswith(phase))
    if in_flight >= max_concurrent:
        return BUSY
    placeholder = asyncio.get_running_loop().create_future()
    _slot_placeholders[phase + str(media_id)] = placeholder
    return None


def _fill_slot(media_id: int, phase: str, task: asyncio.Task, registry: Dict[int, asyncio.Task]):
    """占位坑位填入真实 Task"""
    _slot_placeholders.pop(phase + str(media_id), None)
    registry[media_id] = task
    task.add_done_callback(lambda t: registry.pop(media_id, None))


def _release_slot(media_id: int, phase: str):
    """创建任务失败/预检未过时释放占位"""
    _slot_placeholders.pop(phase + str(media_id), None)

# commit 并发单独限制：瓶颈是 Neo4j 写 + per-user 锁排队，不需要多
MAX_CONCURRENT_COMMITS = 2


async def _heartbeat_with_attempt(media_id: int, attempt: int, phase: str = "analyze"):
    """阶段心跳：刷 updated_at + attempt（watcher 校验心跳归属当前 attempt，防僵尸假心跳）
    #78：条件写入，发现自己是僵尸（attempt 不匹配）立即自我中止"""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            ok = await _set_task_if_owner(media_id, attempt)
            if not ok:
                _abort_zombie(media_id, attempt, phase)
                return
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"[TASK] heartbeat error media_id={media_id}: {e}")


def _precheck_fresh_running(state: Dict[str, str], running_status: str, attempt: int) -> bool:
    """Redis 显示该阶段进行中 + 心跳新鲜 + attempt 归属当前 → 别人在跑"""
    if state.get("status") != running_status:
        return False
    try:
        updated_at = int(state.get("updated_at", "0"))
    except ValueError:
        return False
    if time.time() - updated_at >= FRESH_HEARTBEAT_S:
        return False
    try:
        state_attempt = int(state.get("attempt", "-1"))
    except ValueError:
        state_attempt = -1
    # attempt 不匹配说明心跳是旧执行体写的，不算"有人在跑"
    return state_attempt in (-1, attempt)


async def start_analyze(media_id: int, video_url: str, attempt: int = 1, user_id: int = None,
                        upload_time_ms: int = None) -> str:
    """投递 analyze 后台任务（解析+抽取，无锁并发）
    upload_time_ms: #84 视频真实上传时间（毫秒戳），写入 Neo4j Media.uploaded_at"""
    # 同步占坑（先于任何 await）：防批量消息交错通过并发检查
    denied = _reserve_slot(_analyze_running, media_id, "analyze", MAX_CONCURRENT_BUILDS)
    if denied:
        logger.info(f"[TASK] media_id={media_id} analyze {denied.lower()}")
        return denied

    state = await get_task_state(media_id)
    if _precheck_fresh_running(state, "ANALYZING", attempt):
        logger.info(f"[TASK] media_id={media_id} fresh ANALYZING in Redis, skip")
        _release_slot(media_id, "analyze")
        return DUPLICATE

    group_id = f"user_{user_id}"
    try:
        await _set_task(media_id, attempt=attempt, group_id=group_id, video_url=video_url)
        task = asyncio.create_task(_run_analyze(media_id, video_url, attempt, group_id, upload_time_ms))
        _fill_slot(media_id, "analyze", task, _analyze_running)
    except Exception:
        _release_slot(media_id, "analyze")
        raise
    logger.info(f"[TASK] media_id={media_id} analyze queued (attempt={attempt})")
    return QUEUED


async def _run_analyze(media_id: int, video_url: str, attempt: int, group_id: str,
                       upload_time_ms: int = None):
    from app.services.video_processing_pipeline import VideoProcessingPipeline
    from app.clients import java_callback

    # trace_id（#67）：串联日志/Redis/回调，跨重启可溯源（a=analyze, c=commit）
    trace_id = f"kg-{media_id}-a{attempt}"
    t0 = time.time()
    heartbeat = asyncio.create_task(_heartbeat_with_attempt(media_id, attempt, phase="analyze"))
    try:
        # 状态推进（Java 写 MySQL + 刷 Redis 快照）；409 = 任务已被取消/推进，中止
        await asyncio.to_thread(java_callback.transition_analyzing, media_id)
        await _set_task(media_id, attempt=attempt, trace_id=trace_id)

        pipeline = VideoProcessingPipeline()

        async def analyze_progress(done: int, total: int):
            # 进度更新兼任心跳（条件写入刷新 updated_at）；#78 僵尸检出即中止
            ok = await _set_task_if_owner(media_id, attempt, progress=f"{done}/{total}",
                                          message=f"片段抽取 {done}/{total}")
            if not ok:
                _abort_zombie(media_id, attempt, "analyze")

        result = await pipeline.analyze(video_url, media_id, progress_cb=analyze_progress,
                                        group_id=group_id, uploaded_at_ms=upload_time_ms)
        # 阶段耗时打点（#67）：随 stats 落 MySQL kg_build_tasks.stats，可查询可聚合
        stats = f"{result.get('statistics') or ''} | trace={trace_id} analyze_s={time.time()-t0:.1f}"
        logger.info(f"[TASK] media_id={media_id} analyze done: {stats}")

        # analyze 完成回调：Java 原子完成「MySQL → ANALYZED + 发 kg-commit」
        await asyncio.to_thread(java_callback.analyzed, media_id, stats)

    except java_callback.CallbackRejected as e:
        logger.info(f"[TASK] media_id={media_id} analyze aborted by state rejection: {e}")
    except Exception as e:
        code, retryable = java_callback.classify_error(e)
        logger.error(f"[TASK] media_id={media_id} ANALYZE_FAILED (attempt={attempt}, "
                     f"code={code}, retryable={retryable}): {e}", exc_info=True)
        await asyncio.to_thread(java_callback.failed, media_id, "analyze", code,
                                f"analyze 失败: {e}", retryable)
    finally:
        heartbeat.cancel()


async def start_commit(media_id: int, attempt: int = 1, user_id: int = None) -> str:
    """投递 commit 后台任务（消歧→入库→社区，自持 per-user 写锁）"""
    denied = _reserve_slot(_commit_running, media_id, "commit", MAX_CONCURRENT_COMMITS)
    if denied:
        logger.info(f"[TASK] media_id={media_id} commit {denied.lower()}")
        return denied

    state = await get_task_state(media_id)
    if _precheck_fresh_running(state, "COMMITTING", attempt):
        logger.info(f"[TASK] media_id={media_id} fresh COMMITTING in Redis, skip")
        _release_slot(media_id, "commit")
        return DUPLICATE

    group_id = f"user_{user_id}"
    try:
        await _set_task(media_id, attempt=attempt, group_id=group_id)
        task = asyncio.create_task(_run_commit(media_id, attempt, group_id))
        _fill_slot(media_id, "commit", task, _commit_running)
    except Exception:
        _release_slot(media_id, "commit")
        raise
    logger.info(f"[TASK] media_id={media_id} commit queued (attempt={attempt})")
    return QUEUED


async def _run_commit(media_id: int, attempt: int, group_id: str):
    from app.services.video_processing_pipeline import VideoProcessingPipeline
    from app.clients import java_callback

    trace_id = f"kg-{media_id}-c{attempt}"
    t0 = time.time()
    heartbeat = asyncio.create_task(_heartbeat_with_attempt(media_id, attempt, phase="commit"))
    try:
        # 先推进 COMMITTING 再等锁：等锁期间心跳照跳，watcher 不会误判
        await asyncio.to_thread(java_callback.transition_committing, media_id)
        # 清掉 analyze 阶段的残留进度（如 32/32），防止前端把旧进度误读为完成
        await _set_task(media_id, attempt=attempt, progress="", trace_id=trace_id)

        pipeline = VideoProcessingPipeline()

        async def commit_progress(stage: str):
            # 阶段消息里带 x/y 进度的（如"社区增量更新 97/244"），拆出 progress 字段给前端
            import re as _re
            m = _re.search(r'(\d+/\d+)\s*$', stage)
            if m:
                ok = await _set_task_if_owner(media_id, attempt,
                                              message=f"图谱写入：{stage[:m.start()].strip()}",
                                              progress=m.group(1))
            else:
                ok = await _set_task_if_owner(media_id, attempt, message=f"图谱写入：{stage}")
            if not ok:
                _abort_zombie(media_id, attempt, "commit")

        if settings.kg_two_hop:
            # 两跳链路（#68）：prepare 锁外并行（LLM 全在这里），apply 细粒度实体锁（秒级）
            # 不再持 per-user 图写锁——apply 内部 KgMultiLock 写集锁 + per-group 社区锁
            commit_result = await pipeline.commit_two_hop(media_id, progress_cb=commit_progress,
                                                          group_id=group_id)
        else:
            # 旧链路：per-user 图写锁包住整个 commit
            async with KgLock(GRAPH_WRITE_LOCK_PREFIX + group_id):
                commit_result = await pipeline.commit(media_id, progress_cb=commit_progress,
                                                      group_id=group_id)

        stats = f"{commit_result.get('statistics') or ''} | trace={trace_id} commit_s={time.time()-t0:.1f}"
        await asyncio.to_thread(java_callback.finished, media_id, stats)
        logger.info(f"[TASK] media_id={media_id} SUCCESS: {stats}")

    except java_callback.CallbackRejected as e:
        logger.info(f"[TASK] media_id={media_id} commit aborted by state rejection: {e}")
    except Exception as e:
        code, retryable = java_callback.classify_error(e)
        logger.error(f"[TASK] media_id={media_id} COMMIT_FAILED (attempt={attempt}, "
                     f"code={code}, retryable={retryable}): {e}", exc_info=True)
        await asyncio.to_thread(java_callback.failed, media_id, "commit", code,
                                f"commit 失败: {e}", retryable)
    finally:
        heartbeat.cancel()
