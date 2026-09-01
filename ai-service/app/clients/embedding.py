"""
BGE-M3 Embedding 客户端（批量 + 限流 + 重试）
"""
import requests
from typing import List, Optional
from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import embedding_limiter, call_with_retry

BATCH_SIZE = 32  # SiliconFlow embeddings 单批上限


class EmbeddingClient:
    def __init__(self):
        pass  # 配置改为运行时读取（Redis > .env）

    def _config(self):
        from app.core.runtime_config import runtime_config
        return runtime_config.get('embedding')

    async def embed(self, text: str) -> Optional[List[float]]:
        """获取单条文本的 Embedding"""
        if not text or not text.strip():
            return None
        results = await self.embed_batch([text])
        return results[0] if results else None

    async def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """批量获取 Embedding（自动分批 + 限流 + 重试）"""
        # 空文本占位处理
        cleaned = [t if t and t.strip() else " " for t in texts]
        results: List[Optional[List[float]]] = []

        for i in range(0, len(cleaned), BATCH_SIZE):
            batch = cleaned[i:i + BATCH_SIZE]
            try:
                embeddings = await call_with_retry(
                    self._embed_batch_sync, embedding_limiter, texts=batch
                )
                results.extend(embeddings)
            except Exception as e:
                logger.error(f"Failed to get batch embedding after retries: {e}")
                results.extend([None] * len(batch))

        return results

    def _embed_batch_sync(self, texts: List[str]) -> List[List[float]]:
        """同步批量调用（在线程池中执行）"""
        cfg = self._config()
        url = f"{cfg['base_url']}/embeddings"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }
        data = {"model": cfg['model'], "input": texts}

        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()

        result = response.json()
        # data 按 index 排序对齐输入
        sorted_data = sorted(result['data'], key=lambda x: x['index'])
        return [item['embedding'] for item in sorted_data]
