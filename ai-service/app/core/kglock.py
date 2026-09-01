"""
分布式锁（异步化一期：commit/delete 的图写锁从 Java Redisson 迁移到 Python 自持）
- SET NX PX 获取 + 唯一 token 释放（Lua 校验防误删）
- watchdog 协程自动续期（持锁期间每 ttl/3 续一次，防长任务锁过期）
- 与 Java Redisson 不同 key 体系也不要紧：一期后所有图写操作都在 Python 执行，锁只需 Python 侧互斥
"""
import asyncio
import uuid
from typing import Optional

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import logger

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

_redis_client: Optional[aioredis.Redis] = None


async def _redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            decode_responses=True,
        )
    return _redis_client


class KgLock:
    """
    Redis 分布式锁（async with 使用）：
        async with KgLock("kg:graph:write"):
            await commit(...)
    """

    def __init__(self, name: str, ttl_ms: int = 60_000, wait_timeout_s: float = 7200):
        self.name = name
        self.ttl_ms = ttl_ms
        self.wait_timeout_s = wait_timeout_s  # commit 排队可能很久，默认等 2h
        self.token = uuid.uuid4().hex
        self._watchdog_task: Optional[asyncio.Task] = None

    async def acquire(self) -> bool:
        client = await _redis()
        waited = 0.0
        interval = 1.0
        while True:
            ok = await client.set(self.name, self.token, nx=True, px=self.ttl_ms)
            if ok:
                self._watchdog_task = asyncio.create_task(self._watchdog())
                logger.info(f"[LOCK] acquired {self.name} token={self.token[:8]}")
                return True
            if waited >= self.wait_timeout_s:
                logger.error(f"[LOCK] timeout waiting {self.name} after {waited:.0f}s")
                return False
            await asyncio.sleep(interval)
            waited += interval
            interval = min(interval * 1.5, 10.0)  # 退避，封顶 10s

    async def release(self):
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        try:
            client = await _redis()
            await client.eval(_RELEASE_LUA, 1, self.name, self.token)
            logger.info(f"[LOCK] released {self.name} token={self.token[:8]}")
        except Exception as e:
            logger.warning(f"[LOCK] release failed for {self.name}: {e} (TTL 兜底)")

    async def _watchdog(self):
        """持锁期间定期续期；续期失败（锁丢了）打日志，由调用方感知异常"""
        interval = self.ttl_ms / 1000 / 3
        try:
            while True:
                await asyncio.sleep(interval)
                client = await _redis()
                # 只有自己持有才续期
                current = await client.get(self.name)
                if current != self.token:
                    logger.error(f"[LOCK] watchdog: {self.name} lost (token mismatch), stop renewing")
                    return
                await client.pexpire(self.name, self.ttl_ms)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[LOCK] watchdog error for {self.name}: {e}")

    async def __aenter__(self):
        if not await self.acquire():
            raise TimeoutError(f"acquire lock {self.name} timeout")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


class KgMultiLock:
    """
    实体级细粒度多锁（docs/kg-phase4-commit-optimization.md §7.3）
    对一组 key 排序后逐把加锁：
    - 字典序排序加锁 → 防死锁（所有竞争者按同一顺序拿锁）
    - 任何一把失败 → 释放已获得全部 → 退避重试（不占锁干等）
    - 统一 watchdog：持锁期间对全部 key 按 ttl/3 续期
    用法：
        async with KgMultiLock(["kg:lock:entity:user_2:a", "kg:lock:entity:user_2:b"]):
            await apply(...)
    """

    def __init__(self, names: list[str], ttl_ms: int = 60_000, wait_timeout_s: float = 300):
        # 去重 + 排序（防死锁的核心）
        self.names = sorted(set(names))
        self.ttl_ms = ttl_ms
        self.wait_timeout_s = wait_timeout_s  # 细粒度锁等待应该短，默认 5min
        self.tokens: dict[str, str] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

    async def acquire(self) -> bool:
        if not self.names:
            return True
        client = await _redis()
        waited = 0.0
        interval = 0.5
        while True:
            acquired = await self._try_acquire_all(client)
            if acquired:
                self._watchdog_task = asyncio.create_task(self._watchdog())
                logger.info(f"[MULTILOCK] acquired {len(self.names)} locks, "
                            f"first={self.names[0]} last={self.names[-1]}")
                return True
            if waited >= self.wait_timeout_s:
                logger.error(f"[MULTILOCK] timeout after {waited:.0f}s, {len(self.names)} locks")
                return False
            await asyncio.sleep(interval)
            waited += interval
            interval = min(interval * 1.5, 5.0)

    async def _try_acquire_all(self, client) -> bool:
        """按序逐把尝试；任何一把失败立即释放已获得的全部（防占锁等待死锁）"""
        held = []
        for name in self.names:
            token = uuid.uuid4().hex
            ok = await client.set(name, token, nx=True, px=self.ttl_ms)
            if ok:
                self.tokens[name] = token
                held.append(name)
            else:
                for h in held:
                    await client.eval(_RELEASE_LUA, 1, h, self.tokens[h])
                    self.tokens.pop(h, None)
                return False
        return True

    async def release(self):
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        try:
            client = await _redis()
            for name, token in self.tokens.items():
                await client.eval(_RELEASE_LUA, 1, name, token)
            logger.info(f"[MULTILOCK] released {len(self.tokens)} locks")
            self.tokens.clear()
        except Exception as e:
            logger.warning(f"[MULTILOCK] release error: {e}（TTL 兜底）")

    async def _watchdog(self):
        """持锁期间统一续期全部 key；任何一把 token 不匹配说明锁丢了，停止续期"""
        interval = self.ttl_ms / 1000 / 3
        try:
            while True:
                await asyncio.sleep(interval)
                client = await _redis()
                for name, token in self.tokens.items():
                    current = await client.get(name)
                    if current != token:
                        logger.error(f"[MULTILOCK] watchdog: {name} lost (token mismatch), stop renewing")
                        return
                    await client.pexpire(name, self.ttl_ms)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[MULTILOCK] watchdog error: {e}")

    async def __aenter__(self):
        if not await self.acquire():
            raise TimeoutError(f"acquire multi-lock timeout ({len(self.names)} locks)")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()
