"""
运行时模型配置（Redis 共享，Java 管理端写入，Python 读取）
优先级：Redis（运行时配置）> .env（默认值）
30s 本地缓存，改配置最多 30s 生效，无需重启
"""
import json
import time
from typing import Dict, Optional
from app.core.config import settings
from app.core.logging import logger

REDIS_KEY = "config:models"
CACHE_TTL_SECONDS = 30


class RuntimeConfig:
    def __init__(self):
        self._redis = None
        self._cache: Dict[str, dict] = {}
        self._cache_ts = 0.0

    def _client(self):
        if self._redis is None:
            import redis
            self._redis = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                decode_responses=True
            )
        return self._redis

    def get(self, kind: str) -> Dict[str, str]:
        """
        获取模型配置：{base_url, api_key, model}
        kind: llm / embedding / asr / reranker
        """
        now = time.time()
        if now - self._cache_ts > CACHE_TTL_SECONDS:
            self._reload()
            self._cache_ts = now

        cfg = self._cache.get(kind)
        if cfg and cfg.get('model'):
            return cfg
        return self._default(kind)

    def _reload(self):
        try:
            data = self._client().hgetall(REDIS_KEY)
            self._cache = {k: json.loads(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"[RUNTIME_CONFIG] Redis read failed, use cached/default: {e}")

    def _default(self, kind: str) -> Dict[str, str]:
        """.env 默认值兜底"""
        if kind == 'llm':
            return {
                'base_url': settings.deepseek_base_url,
                'api_key': settings.deepseek_api_key,
                'model': settings.deepseek_model
            }
        if kind == 'embedding':
            return {
                'base_url': settings.siliconflow_base_url,
                'api_key': settings.siliconflow_api_key,
                'model': settings.embedding_model
            }
        if kind == 'asr':
            return {
                'base_url': settings.ai_asr_url,
                'api_key': settings.siliconflow_api_key,
                'model': settings.ai_asr_model
            }
        if kind == 'reranker':
            return {
                'base_url': settings.siliconflow_base_url,
                'api_key': settings.siliconflow_api_key,
                'model': 'BAAI/bge-reranker-v2-m3'
            }
        raise ValueError(f"Unknown config kind: {kind}")


runtime_config = RuntimeConfig()
