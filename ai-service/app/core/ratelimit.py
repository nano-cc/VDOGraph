"""
分布式限流（Redis 令牌桶 + 在途信号量）
多副本共享：所有 Python 实例从同一个 Redis 桶取令牌
- 令牌桶：Lua 原子操作，控制 RPM
- 在途信号量：INCR/DECR + 许可租约（TTL），持有者崩溃自动归还
- 429/网络错误：指数退避重试
"""
import asyncio
import time
import uuid
from typing import Optional
import redis.asyncio as aioredis
from app.core.config import settings
from app.core.logging import logger

# 令牌桶 Lua：原子扣减
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])       -- 每秒补充速率
local capacity = tonumber(ARGV[2])   -- 桶容量
local now = tonumber(ARGV[3])        -- 当前时间（秒，浮点）
local requested = tonumber(ARGV[4])  -- 请求令牌数

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1]) or capacity
local ts = tonumber(data[2]) or now

-- 补充令牌
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, 60)
    return 0  -- 成功
else
    -- 返回需要等待的秒数
    local wait = (requested - tokens) / rate
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, 60)
    return wait
end
"""


class DistributedRateLimiter:
    """
    分布式限流器：令牌桶（速率）+ 在途信号量（并发数）
    """

    def __init__(self,
                 name: str = "siliconflow",
                 rate_per_sec: float = 2.0,      # 令牌补充速率（≈120 RPM，保守）
                 bucket_capacity: int = 8,       # 桶容量（突发余量）
                 max_inflight: int = 10,         # 最大在途并发
                 lease_seconds: int = 120):      # 许可租约
        self.name = name
        self.rate = rate_per_sec
        self.capacity = bucket_capacity
        self.max_inflight = max_inflight
        self.lease_seconds = lease_seconds
        self._redis: Optional[aioredis.Redis] = None
        self._bucket_script = None

    async def _client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                decode_responses=True
            )
            self._bucket_script = self._redis.register_script(_TOKEN_BUCKET_LUA)
        return self._redis

    async def acquire(self, tokens: int = 1):
        """获取许可：先过令牌桶（限速），再占在途名额（限并发）"""
        client = await self._client()

        # 1. 令牌桶
        while True:
            wait = await self._bucket_script(
                keys=[f"rl:{self.name}:bucket"],
                args=[self.rate, self.capacity, time.time(), tokens]
            )
            wait = float(wait)
            if wait <= 0:
                break
            await asyncio.sleep(wait + 0.05)

        # 2. 在途信号量（带租约的占位）
        permit_id = str(uuid.uuid4())
        inflight_key = f"rl:{self.name}:inflight"
        while True:
            count = await client.incr(inflight_key)
            if count == 1:
                await client.expire(inflight_key, self.lease_seconds * 2)
            if count <= self.max_inflight:
                break
            await client.decr(inflight_key)
            await asyncio.sleep(0.3)

        return permit_id

    async def release(self):
        """释放在途名额"""
        try:
            client = await self._client()
            await client.decr(f"rl:{self.name}:inflight")
        except Exception as e:
            logger.warning(f"[RATELIMIT] release failed: {e}")

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        await self.release()


# 全局限流器单例（LLM 与 embedding 分开限速）
llm_limiter = DistributedRateLimiter("llm", rate_per_sec=2.0, bucket_capacity=8, max_inflight=10)
embedding_limiter = DistributedRateLimiter("embedding", rate_per_sec=10.0, bucket_capacity=20, max_inflight=10)


async def call_with_retry(func, limiter: DistributedRateLimiter, max_retries: int = 3, **kwargs):
    """
    限流 + 退避重试包裹外部 API 调用
    func: 同步阻塞函数（自动 to_thread）
    """
    last_error = None
    for attempt in range(max_retries + 1):
        await limiter.acquire()
        try:
            return await asyncio.to_thread(func, **kwargs)
        except Exception as e:
            last_error = e
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status and 400 <= status < 500 and status != 429:
                raise  # 4xx（非 429）不重试
            backoff = min(2 ** attempt * 2, 30) + (time.time() % 1)  # 指数退避 + jitter
            logger.warning(f"[RATELIMIT] call failed (attempt {attempt+1}/{max_retries+1}), retry in {backoff:.1f}s: {e}")
            await asyncio.sleep(backoff)
        finally:
            await limiter.release()
    raise last_error
