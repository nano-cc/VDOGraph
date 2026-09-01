"""
图谱检索层（框架无关，Graphiti 式多路混合检索）
- local_search: 实体三路召回（名称向量 + 描述向量 + BM25 全文）+ 关系两路召回，RRF 融合
- global_search: 社区两路召回（摘要向量 + BM25 全文），RRF 融合
- segment_search: 片段两路召回（原文向量 + BM25 全文），RRF 融合
所有结果带 citations（片段时间戳/媒体/证据帧），保证可溯源
"""
import time
from typing import List, Dict, Tuple, Hashable
from app.clients.neo4j_client import Neo4jClient
from app.clients.embedding import EmbeddingClient
from app.clients.reranker import RerankerClient
from app.core.logging import logger

# RRF 常数（Graphiti 用 1，论文标准默认 60；我们取 60 让排名差异更平滑）
RRF_K = 60


def rrf_fuse(rank_lists: List[List[Tuple[Hashable, float]]], k: int = RRF_K) -> List[Tuple[Hashable, float]]:
    """
    Reciprocal Rank Fusion：多路召回结果按排名融合
    输入：多路结果，每路是 [(key, score), ...] 按分数降序
    返回：[(key, rrf_score), ...] 按融合分降序
    """
    scores: Dict[Hashable, float] = {}
    for rank_list in rank_lists:
        for rank, (key, _) in enumerate(rank_list):
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank + k)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


class GraphRetriever:
    def __init__(self, use_rerank: bool = True):
        self.neo4j_client = Neo4jClient()
        self.embedding_client = EmbeddingClient()
        self.reranker = RerankerClient()
        self.use_rerank = use_rerank  # A/B 测试开关：False 时 RRF 融合序直接截断

    async def _rerank(self, query: str, candidates: List[Dict], text_fn, top_k: int) -> List[Dict]:
        """
        cross-encoder 精排：对 RRF 融合后的候选重排
        失败/为空时回退到融合序
        """
        if not candidates:
            return candidates
        if not self.use_rerank:
            return candidates[:top_k]
        docs = [text_fn(c) for c in candidates]
        indices = await self.reranker.rerank(query, docs, top_k=top_k)
        if indices is None:
            return candidates[:top_k]
        return [candidates[i] for i in indices]

    async def local_search(self, query: str, group_id: str, top_k: int = 5, media_id: int = None) -> Dict:
        """
        实体级检索：三路召回（名称向量 + 描述向量 + BM25）RRF 融合（全部 group 内闭包）
        -> 一跳关系扩展 + 关系两路直接召回（向量 + BM25）
        media_id 可选：限定单个视频范围内（单视频问答）
        """
        start = time.time()
        query_embedding = await self.embedding_client.embed(query)

        # 1. 实体三路召回
        entity_by_id = {}
        rank_lists = []

        if query_embedding:
            name_hits = self.neo4j_client.find_similar_entities(query_embedding, group_id, top_k=top_k * 2, threshold=0.5, media_id=media_id)
            desc_hits = self.neo4j_client.search_entities_by_description(query_embedding, group_id, top_k=top_k * 2, threshold=0.6, media_id=media_id)
            for hits in (name_hits, desc_hits):
                rank_lists.append([(h['entity']['id'], h['score']) for h in hits])
                for h in hits:
                    entity_by_id[h['entity']['id']] = h['entity']

        fulltext_hits = self.neo4j_client.search_entities_fulltext(query, group_id, top_k=top_k * 2, media_id=media_id)
        rank_lists.append([(h['entity']['id'], h['score']) for h in fulltext_hits])
        for h in fulltext_hits:
            entity_by_id[h['entity']['id']] = h['entity']

        # 第四路：BFS 图遍历（Graphiti 式：用前三路命中的头部实体当起点扩散）
        seed_ids = [eid for eid, _ in rrf_fuse(rank_lists)[:3]]
        bfs_hits = self.neo4j_client.entity_bfs_search(seed_ids, group_id, depth=2, limit=top_k * 2, media_id=media_id)
        if bfs_hits:
            rank_lists.append([(h['entity']['id'], h['score']) for h in bfs_hits])
            for h in bfs_hits:
                entity_by_id.setdefault(h['entity']['id'], h['entity'])

        fused = rrf_fuse(rank_lists)
        candidates = [entity_by_id[eid] for eid, _ in fused[:top_k * 2]]
        # cross-encoder 精排（实体名 + 描述作为文档）
        top_entities = await self._rerank(
            query, candidates,
            lambda e: f"{e['name']} ({e.get('type', '')}): {e.get('description', '')}",
            top_k
        )

        logger.info(f"[RETRIEVER] local_search '{query}': {len(top_entities)} entities after RRF+rerank "
                    f"(vector_name={len(rank_lists[0]) if rank_lists else 0}, "
                    f"fulltext={len(fulltext_hits)})")

        # 2. 关系两路直接召回（向量 + BM25，RRF 融合）
        rel_by_key = {}
        rel_rank_lists = []
        if query_embedding:
            rel_vec_hits = self.neo4j_client.search_relationships_by_vector(query_embedding, group_id, top_k=top_k * 2, media_id=media_id)
            rel_rank_lists.append([(self._rel_key(r), r['score']) for r in rel_vec_hits])
            for r in rel_vec_hits:
                rel_by_key[self._rel_key(r)] = r
        rel_ft_hits = self.neo4j_client.search_relationships_fulltext(query, group_id, top_k=top_k * 2, media_id=media_id)
        rel_rank_lists.append([(self._rel_key(r), r['score']) for r in rel_ft_hits])
        for r in rel_ft_hits:
            rel_by_key[self._rel_key(r)] = r

        rel_fused = rrf_fuse(rel_rank_lists)
        rel_candidates = [rel_by_key[key] for key, _ in rel_fused[:top_k * 2]]
        direct_rels = await self._rerank(
            query, rel_candidates,
            lambda r: f"{r['source_name']} -> {r['target_name']}: {r['description']}",
            top_k
        )

        # 3. 组装上下文：实体 + 一跳关系 + 直接召回关系
        # 单视频模式：来源/引用一并限定在该视频（实体可能同时属于多个视频，只取本视频的证据）
        mprefix = f"media_{media_id}_" if media_id is not None else None
        contexts = []
        citations = {}
        for entity in top_entities:
            contexts.append(
                f"实体「{entity['name']}」({entity.get('type', '')}): {entity.get('description', '')}"
            )
            for rel in self.neo4j_client.get_entity_relationships(entity['id'], top_k=3):
                sids = rel.get('source_segment_ids') or []
                if mprefix:
                    sids = [sid for sid in sids if sid.startswith(mprefix)]
                    if not sids:
                        continue  # 该关系在本视频无来源，不带入
                contexts.append(
                    f"关系: {rel['source_name']} -> {rel['target_name']}: {rel['description']}"
                )
                for sid in sids:
                    citations[sid] = None
            for source in self.neo4j_client.get_entity_sources(entity['id']):
                if media_id is not None and source['segment'].get('media_id') != media_id:
                    continue
                citations[source['segment']['id']] = None

        for rel in direct_rels:
            sids = rel.get('source_segment_ids') or []
            if mprefix:
                sids = [sid for sid in sids if sid.startswith(mprefix)]
            contexts.append(
                f"关系: {rel['source_name']} -> {rel['target_name']}: {rel['description']}"
            )
            for sid in sids:
                citations[sid] = None

        citation_list = self._build_citations(list(citations.keys()))

        duration = (time.time() - start) * 1000
        logger.info(f"[RETRIEVER] local_search completed: {len(contexts)} contexts, {len(citation_list)} citations, {duration:.2f}ms")
        return {'contexts': contexts, 'citations': citation_list}

    async def global_search(self, query: str, group_id: str, top_k: int = 5, media_id: int = None) -> Dict:
        """
        社区级检索：两路召回（摘要向量 + BM25 全文）RRF 融合（group 内闭包）
        media_id 可选：限定覆盖单个视频的社区（单视频问答）
        """
        start = time.time()
        query_embedding = await self.embedding_client.embed(query)

        community_by_id = {}
        rank_lists = []
        if query_embedding:
            vec_hits = self.neo4j_client.search_communities(query_embedding, group_id, top_k=top_k * 2, media_id=media_id)
            rank_lists.append([(h['community']['id'], h['score']) for h in vec_hits])
            for h in vec_hits:
                community_by_id[h['community']['id']] = h['community']

        ft_hits = self.neo4j_client.search_communities_fulltext(query, group_id, top_k=top_k * 2, media_id=media_id)
        rank_lists.append([(h['community']['id'], h['score']) for h in ft_hits])
        for h in ft_hits:
            community_by_id[h['community']['id']] = h['community']

        fused = rrf_fuse(rank_lists)
        comm_candidates = [community_by_id[cid] for cid, _ in fused[:top_k * 2]]
        top_communities = await self._rerank(
            query, comm_candidates,
            lambda c: c.get('summary', ''),
            top_k
        )

        logger.info(f"[RETRIEVER] global_search '{query}': {len(top_communities)} communities after RRF+rerank")

        contexts = []
        citations = {}
        for community in top_communities:
            contexts.append(
                f"主题摘要（{community.get('entity_count', 0)} 个相关实体）: {community.get('summary', '')}"
            )
            for segment in self.neo4j_client.get_community_segments(community['id']):
                if media_id is not None and segment.get('media_id') != media_id:
                    continue
                citations[segment['id']] = None

        citation_list = self._build_citations(list(citations.keys()))

        duration = (time.time() - start) * 1000
        logger.info(f"[RETRIEVER] global_search completed: {len(contexts)} contexts, {len(citation_list)} citations, {duration:.2f}ms")
        return {'contexts': contexts, 'citations': citation_list}

    async def segment_search(self, query: str, group_id: str, top_k: int = 5, media_id: int = None) -> Dict:
        """
        片段级检索：两路召回（原文向量 + BM25 全文）RRF 融合（group 内闭包）
        media_id 可选：限定单个视频的片段（单视频问答）
        """
        start = time.time()
        query_embedding = await self.embedding_client.embed(query)

        segment_by_id = {}
        rank_lists = []
        if query_embedding:
            vec_hits = self.neo4j_client.search_segments(query_embedding, group_id, top_k=top_k * 2, threshold=0.4, media_id=media_id)
            rank_lists.append([(h['segment']['id'], h['score']) for h in vec_hits])
            for h in vec_hits:
                segment_by_id[h['segment']['id']] = h['segment']

        ft_hits = self.neo4j_client.search_segments_fulltext(query, group_id, top_k=top_k * 2, media_id=media_id)
        rank_lists.append([(h['segment']['id'], h['score']) for h in ft_hits])
        for h in ft_hits:
            segment_by_id[h['segment']['id']] = h['segment']

        fused = rrf_fuse(rank_lists)
        seg_candidates = [segment_by_id[sid] for sid, _ in fused[:top_k * 2]]
        top_segments = await self._rerank(
            query, seg_candidates,
            lambda s: (s.get('transcript') or '')[:300],
            top_k
        )

        logger.info(f"[RETRIEVER] segment_search '{query}': {len(top_segments)} segments after RRF+rerank")

        contexts = []
        citations = []
        for segment in top_segments:
            contexts.append(
                f"片段原文（{segment['start_ms']//1000}s-{segment['end_ms']//1000}s）: {segment.get('transcript', '')[:300]}"
            )
            citations.append(self._segment_to_citation(segment))

        duration = (time.time() - start) * 1000
        logger.info(f"[RETRIEVER] segment_search completed: {len(contexts)} contexts, {duration:.2f}ms")
        return {'contexts': contexts, 'citations': citations}

    def _rel_key(self, rel: Dict) -> str:
        return f"{rel['source_id']}|{rel['target_id']}|{rel['description'][:50]}"

    def _build_citations(self, segment_ids: List[str]) -> List[Dict]:
        """根据片段 id 批量补引用信息"""
        if not segment_ids:
            return []
        with self.neo4j_client.driver.session() as session:
            result = session.run("""
                MATCH (s:Segment) WHERE s.id IN $ids
                RETURN s
            """, {'ids': segment_ids})
            return [self._segment_to_citation(dict(record['s'])) for record in result]

    def _segment_to_citation(self, segment: Dict) -> Dict:
        return {
            'segment_id': segment['id'],
            'media_id': segment.get('media_id'),
            'start_ms': segment.get('start_ms'),
            'end_ms': segment.get('end_ms'),
            'frame_urls': segment.get('frame_urls', [])[:3],  # 最多带 3 帧证据
            'transcript_excerpt': (segment.get('transcript') or '')[:100]
        }
