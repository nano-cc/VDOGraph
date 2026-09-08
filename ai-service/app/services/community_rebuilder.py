"""
全量 Leiden 社区重建服务（#83）

背景：增量社区归属（邻居投票→向量+LLM→singleton 保底）在稀疏图上大量掉 singleton，
global search 依赖多实体社区摘要。定期全量重跑 Leiden 纠偏。

并发安全：与增量社区更新共用 per-group 社区锁（kg:lock:community:{group}），
重建期间 commit 的社区更新排队；重建是「读全图→算→整体替换」，替换本身很快，
长时间计算（Leiden+摘要）在锁外完成，缩短持锁时间。
"""
import asyncio
import time
from typing import Dict

from app.clients.neo4j_client import Neo4jClient
from app.clients.embedding import EmbeddingClient
from app.services.community_detector import CommunityDetector
from app.core.kglock import KgLock
from app.core.logging import logger

COMMUNITY_LOCK_PREFIX = "kg:lock:community:"


class CommunityRebuilder:
    def __init__(self):
        self.neo = Neo4jClient()
        self.embedding = EmbeddingClient()

    async def rebuild(self, group_id: str, min_singleton_ratio: float = 0.0) -> Dict:
        """
        对 group 全量重建社区。
        min_singleton_ratio > 0 时：singleton 占比低于该阈值则跳过（没必要重建）。
        返回统计信息。
        """
        t0 = time.time()
        entities, relationships = self._load_graph(group_id)
        if not entities:
            return {'status': 'skipped', 'reason': 'no entities', 'group_id': group_id}

        if min_singleton_ratio > 0:
            ratio = self._singleton_ratio(group_id)
            if ratio < min_singleton_ratio:
                return {'status': 'skipped', 'reason': f'singleton ratio {ratio:.1%} < {min_singleton_ratio:.0%}',
                        'group_id': group_id}

        # 锁外计算（分钟级）：Leiden 划分 + LLM 摘要 + 向量化
        detector = CommunityDetector()
        result = await detector.detect(entities, relationships)
        communities = result['communities']
        logger.info(f"[COMMUNITY-REBUILD] {group_id}: detect done {result['statistics']}")

        summaries = [(c.summary or ' ') for c in communities]
        embeddings = await self.embedding.embed_batch(summaries)

        # 锁内整体替换（秒级）：与增量社区更新互斥
        async with KgLock(COMMUNITY_LOCK_PREFIX + group_id):
            self._replace_communities(group_id, communities, embeddings)

        duration = time.time() - t0
        stats = {
            'status': 'success', 'group_id': group_id,
            'entities': len(entities), 'relationships': len(relationships),
            'communities': len(communities),
            'algorithm': result['statistics'].get('algorithm'),
            'duration_s': round(duration, 1),
        }
        logger.info(f"[COMMUNITY-REBUILD] {group_id} done: {stats}")
        return stats

    def _load_graph(self, group_id: str):
        with self.neo.driver.session() as s:
            entities = s.run("""
                MATCH (e:Entity {group_id: $g})
                OPTIONAL MATCH (e)-[:MENTIONED_IN]->(sg:Segment)
                RETURN e.id AS id, e.name AS name, e.type AS type,
                       e.description AS description,
                       coalesce(e.source_count, 1) AS source_count,
                       collect(DISTINCT sg.id) AS seg_ids
            """, g=group_id).data()
            for e in entities:
                e['sources'] = [{'segment_id': sid} for sid in e.pop('seg_ids') if sid]
            relationships = s.run("""
                MATCH (a:Entity {group_id: $g})-[r:RELATES_TO]->(b:Entity {group_id: $g})
                RETURN a.id AS source_entity_id, b.id AS target_entity_id,
                       a.name AS source_entity_name, b.name AS target_entity_name,
                       r.description AS description, coalesce(r.strength, 5) AS strength
            """, g=group_id).data()
        return entities, relationships

    def _singleton_ratio(self, group_id: str) -> float:
        with self.neo.driver.session() as s:
            r = s.run("""
                MATCH (c:Community {group_id: $g})
                OPTIONAL MATCH (c)<-[:BELONGS_TO]-(e:Entity)
                WITH c, count(e) AS sz
                RETURN count(*) AS total, sum(CASE WHEN sz<=1 THEN 1 ELSE 0 END) AS singles
            """, g=group_id).single()
        return r['singles'] / r['total'] if r['total'] else 0.0

    def _replace_communities(self, group_id: str, communities, embeddings):
        with self.neo.driver.session() as s:
            s.run("MATCH (c:Community {group_id: $g}) DETACH DELETE c", g=group_id)
            s.run("""
                UNWIND $rows AS row
                MERGE (c:Community {id: row.id})
                SET c.group_id = $g, c.level = row.level, c.parent_id = row.parent_id,
                    c.entity_count = row.entity_count, c.relationship_count = row.relationship_count,
                    c.summary = row.summary, c.findings = row.findings,
                    c.summary_embedding = row.summary_embedding, c.updated_at = datetime()
            """, rows=[{
                'id': f"{group_id}_leiden_{c.id}",
                'level': c.level, 'parent_id': c.parent_id,
                'entity_count': c.entity_count, 'relationship_count': c.relationship_count,
                'summary': c.summary or '', 'findings': c.findings or [],
                'summary_embedding': emb,
            } for c, emb in zip(communities, embeddings)], g=group_id)

            links = [(f"{group_id}_leiden_{c.id}", e.id) for c in communities for e in c.entities]
            BATCH = 500
            for i in range(0, len(links), BATCH):
                s.run("""
                    UNWIND $rows AS row
                    MATCH (e:Entity {id: row.eid})
                    MATCH (c:Community {id: row.cid})
                    MERGE (e)-[:BELONGS_TO]->(c)
                """, rows=[{'eid': eid, 'cid': cid} for cid, eid in links[i:i+BATCH]])

            # Community -[:CONTAINS]-> Segment（global search 溯源链，漏建则 global 检索零引用）
            seg_links = [(f"{group_id}_leiden_{c.id}", sid)
                         for c in communities for sid in (c.source_segments or [])]
            for i in range(0, len(seg_links), BATCH):
                s.run("""
                    UNWIND $rows AS row
                    MATCH (c:Community {id: row.cid})
                    MATCH (sg:Segment {id: row.sid})
                    MERGE (c)-[:CONTAINS]->(sg)
                """, rows=[{'cid': cid, 'sid': sid} for cid, sid in seg_links[i:i+BATCH]])


# 进程内防重：同一 group 同时只能有一个重建
_rebuild_running: Dict[str, asyncio.Task] = {}


async def start_rebuild(group_id: str, min_singleton_ratio: float = 0.0) -> str:
    """投递后台重建，返回 QUEUED/DUPLICATE"""
    existing = _rebuild_running.get(group_id)
    if existing is not None and not existing.done():
        return "DUPLICATE"

    async def _run():
        try:
            await CommunityRebuilder().rebuild(group_id, min_singleton_ratio)
        except Exception as e:
            logger.error(f"[COMMUNITY-REBUILD] {group_id} failed: {e}", exc_info=True)

    task = asyncio.create_task(_run())
    _rebuild_running[group_id] = task
    task.add_done_callback(lambda t: _rebuild_running.pop(group_id, None))
    return "QUEUED"
