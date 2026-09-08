"""
BGE Reranker 客户端（SiliconFlow cross-encoder 精排）
"""
import time
import requests
from typing import List, Optional
from app.core.config import settings
from app.core.logging import logger


class RerankerClient:
    def __init__(self):
        pass  # 配置改为运行时读取（Redis > .env）

    def _config(self):
        from app.core.runtime_config import runtime_config
        return runtime_config.get('reranker')

    async def rerank(self, query: str, documents: List[str], top_k: int = 5) -> Optional[List[int]]:
        """
        对候选文档做 cross-encoder 精排
        返回：按相关性降序的文档下标列表（失败返回 None，调用方回退到融合序）
        """
        if not documents:
            return []

        try:
            start = time.time()
            cfg = self._config()
            # path 可配：SiliconFlow=/rerank，百炼 qwen3-rerank=/reranks（compatible-api）
            response = requests.post(
                f"{cfg['base_url']}{cfg.get('path', '/rerank')}",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": cfg['model'],
                    "query": query,
                    "documents": documents,
                    "top_n": min(top_k, len(documents))
                },
                timeout=30
            )
            response.raise_for_status()

            results = response.json()['results']
            # results: [{index, relevance_score}, ...] 已按分数降序
            indices = [r['index'] for r in results]

            duration = (time.time() - start) * 1000
            logger.info(f"[RERANK] query='{query[:30]}' {len(documents)} docs -> top {len(indices)}, {duration:.2f}ms")
            return indices

        except Exception as e:
            logger.warning(f"[RERANK] failed, fallback to fusion order: {e}")
            return None
