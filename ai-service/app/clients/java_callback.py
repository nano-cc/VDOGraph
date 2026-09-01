"""
Python → Java 内部回调客户端（#66 方案 C）
Python 不直连 MQ：analyze 完成/阶段转换/失败/成功都回调 Java /internal/kg/*，
由 Java 原子完成「写 MySQL + 删 Redis + 发下一阶段 MQ」。

- 网络/5xx 错误：指数退避重试 3 次（回调失败最坏等 Watcher 心跳超时兜底，但尽量自愈）
- 409：状态前置条件不满足（任务已被取消/已被别的执行体推进），调用方必须中止当前执行体
"""
import time
from typing import Optional

import requests

from app.core.config import settings
from app.core.logging import logger

_CALLBACK_TIMEOUT_S = 10
_MAX_RETRY = 3


class CallbackRejected(Exception):
    """Java 返回 409：状态前置条件不满足，当前执行体应中止"""
    pass


def _headers() -> dict:
    key = settings.kg_internal_api_key or settings.ai_service_api_key
    return {"Content-Type": "application/json", "X-Internal-Key": key}


def _post(path: str, payload: dict, allow_reject: bool = True) -> dict:
    url = settings.java_backend_url.rstrip("/") + path
    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRY + 1):
        try:
            resp = requests.post(url, json=payload, headers=_headers(), timeout=_CALLBACK_TIMEOUT_S)
            if resp.status_code == 409:
                if allow_reject:
                    raise CallbackRejected(f"callback rejected (409): {path} media_id={payload.get('media_id')}")
                return {"accepted": False}
            if resp.status_code >= 500:
                raise RuntimeError(f"callback {path} HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json() if resp.text else {}
        except CallbackRejected:
            raise
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRY:
                time.sleep(0.8 * (2 ** (attempt - 1)))
    raise RuntimeError(f"callback {path} failed after {_MAX_RETRY} attempts: {last_err}")


def transition_analyzing(media_id: int, message: str = "解析与抽取中"):
    """QUEUED/ANALYZE_FAILED → ANALYZING。409 → CallbackRejected（中止执行体）"""
    return _post("/internal/kg/state", {"media_id": media_id, "to": "ANALYZING", "message": message})


def transition_committing(media_id: int, message: str = "图谱写入中"):
    """ANALYZED/COMMIT_FAILED → COMMITTING。409 → CallbackRejected（中止执行体）"""
    return _post("/internal/kg/state", {"media_id": media_id, "to": "COMMITTING", "message": message})


def analyzed(media_id: int, stats: str = ""):
    """ANALYZING → ANALYZED，Java 收到后原子发 kg-commit。409 → CallbackRejected"""
    return _post("/internal/kg/analyzed", {"media_id": media_id, "stats": stats})


def finished(media_id: int, stats: str = ""):
    """COMMITTING → SUCCESS。409 → CallbackRejected"""
    return _post("/internal/kg/finished", {"media_id": media_id, "stats": stats})


def failed(media_id: int, error_phase: str, error_code: str, message: str, retryable: bool):
    """→ ANALYZE_FAILED / COMMIT_FAILED。失败回调本身失败只记日志（Watcher 心跳超时兜底）"""
    try:
        return _post("/internal/kg/failed", {
            "media_id": media_id,
            "error_phase": error_phase,
            "error_code": error_code,
            "message": message[:1000] if message else "",
            "retryable": retryable,
        }, allow_reject=False)
    except Exception as e:
        logger.error(f"[CALLBACK] failed 回调失败 media_id={media_id}: {e}（等 Watcher 心跳超时兜底）")
        return {"accepted": False}


def classify_error(e: Exception) -> tuple[str, bool]:
    """
    错误分类（#66 P0-5）：永久错误不重试，其余一律视为瞬时可重试。
    返回 (error_code, retryable)

    原则：默认 retryable=True（瞬时），只有明确的永久信号才判不可重试。
    注意异常类型不可靠——ValueError 可能是"所有 ASR 分片 503 失败"的包装（瞬时），
    所以先看消息文本里的瞬时信号，再看永久信号。
    """
    text = str(e).lower()

    # 瞬时信号优先：下游 429/5xx/超时/限流，重试有意义
    transient_markers = (
        "429", "502", "503", "504", "timeout", "timed out",
        "transient", "限流", "重试", "connection", "temporarily",
    )
    for marker in transient_markers:
        if marker in text:
            return ("TRANSIENT_" + type(e).__name__.upper()[:50], True)

    # 永久信号：文件不存在/视频损坏/参数非法，重试一万次也一样
    permanent_markers = (
        "no such", "not found", "不存在",
        "ffprobe", "无法解析为有效视频", "视频内容校验失败",
        "corrupt",
    )
    if isinstance(e, FileNotFoundError):
        return ("PERMANENT_FILENOTFOUND", False)
    for marker in permanent_markers:
        if marker in text:
            return ("PERMANENT_" + marker.upper().replace(" ", "_")[:40], False)

    # 未知错误默认可重试（保守策略：宁可多投一次，不漏任务）
    return (type(e).__name__.upper()[:60], True)
