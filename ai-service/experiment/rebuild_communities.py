#!/usr/bin/env python3
"""全量 Leiden 社区重建（#82）：对指定 group 重跑社区划分并整体替换

背景：增量社区归属（邻居投票→向量+LLM→singleton 保底）实测 99% 掉到 singleton，
global search 依赖多实体社区摘要，需要全量 Leiden 纠偏。

步骤：读 group 全部实体/关系 → CommunityDetector.detect()（Leiden+摘要）
→ 删除旧 Community 节点（DETACH）→ 批量写新社区 + BELONGS_TO 边。

用法：python experiment/rebuild_communities.py [group_id]
"""
import asyncio
import sys

sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')

from app.clients.neo4j_client import Neo4jClient
from app.clients.embedding import EmbeddingClient
from app.services.community_detector import CommunityDetector
from app.core.logging import logger

GROUP_ID = sys.argv[1] if len(sys.argv) > 1 else 'user_4'


async def main():
    neo = Neo4jClient()

    # 1. 读全量实体/关系
    with neo.driver.session() as s:
        entities = s.run("""
            MATCH (e:Entity {group_id: $g})
            OPTIONAL MATCH (e)-[:MENTIONED_IN]->(sg:Segment)
            RETURN e.id AS id, e.name AS name, e.type AS type,
                   e.description AS description,
                   coalesce(e.source_count, 1) AS source_count,
                   collect(DISTINCT sg.id) AS seg_ids
        """, g=GROUP_ID).data()
        for e in entities:
            e['sources'] = [{'segment_id': sid} for sid in e.pop('seg_ids') if sid]
        relationships = s.run("""
            MATCH (a:Entity {group_id: $g})-[r:RELATES_TO]->(b:Entity {group_id: $g})
            RETURN a.id AS source_entity_id, b.id AS target_entity_id,
                   a.name AS source_entity_name, b.name AS target_entity_name,
                   r.description AS description, coalesce(r.strength, 5) AS strength
        """, g=GROUP_ID).data()
    logger.info(f"[REBUILD] {GROUP_ID}: {len(entities)} entities, {len(relationships)} relationships")
    if not entities:
        print("没有实体，退出")
        return

    # 2. Leiden 全量划分 + LLM 摘要
    detector = CommunityDetector()
    result = await detector.detect(entities, relationships)
    communities = result['communities']
    logger.info(f"[REBUILD] detect done: {result['statistics']}")

    # 3. 摘要向量化（批量）
    ec = EmbeddingClient()
    summaries = [(c.summary or '') for c in communities]
    embeddings = await ec.embed_batch([t if t.strip() else ' ' for t in summaries])

    # 4. 整体替换：删旧社区（BELONGS_TO 边随 DETACH 一起删）
    with neo.driver.session() as s:
        r = s.run("MATCH (c:Community {group_id: $g}) DETACH DELETE c RETURN count(*) AS deleted", g=GROUP_ID)
        logger.info(f"[REBUILD] deleted old communities: {r.single()['deleted']}")

    # 5. 写新社区 + 成员边（批量 UNWIND）
    with neo.driver.session() as s:
        s.run("""
            UNWIND $rows AS row
            MERGE (c:Community {id: row.id})
            SET c.group_id = $g, c.level = row.level, c.parent_id = row.parent_id,
                c.entity_count = row.entity_count, c.relationship_count = row.relationship_count,
                c.summary = row.summary, c.findings = row.findings,
                c.summary_embedding = row.summary_embedding, c.updated_at = datetime()
        """, rows=[{
            'id': f"{GROUP_ID}_leiden_{c.id}",
            'level': c.level, 'parent_id': c.parent_id,
            'entity_count': c.entity_count, 'relationship_count': c.relationship_count,
            'summary': c.summary or '', 'findings': c.findings or [],
            'summary_embedding': emb,
        } for c, emb in zip(communities, embeddings)], g=GROUP_ID)

        links = [(f"{GROUP_ID}_leiden_{c.id}", e.id) for c in communities for e in c.entities]
        BATCH = 500
        for i in range(0, len(links), BATCH):
            s.run("""
                UNWIND $rows AS row
                MATCH (e:Entity {id: row.eid})
                MATCH (c:Community {id: row.cid})
                MERGE (e)-[:BELONGS_TO]->(c)
            """, rows=[{'eid': eid, 'cid': cid} for cid, eid in links[i:i+BATCH]])

    # 6. 验证
    with neo.driver.session() as s:
        stats = s.run("""
            MATCH (c:Community {group_id: $g})
            OPTIONAL MATCH (c)<-[:BELONGS_TO]-(e:Entity)
            WITH c, count(e) AS sz
            RETURN count(*) AS total,
                   sum(CASE WHEN sz<=1 THEN 1 ELSE 0 END) AS singletons,
                   max(sz) AS max_size, avg(sz) AS avg_size
        """, g=GROUP_ID).single()
        print(f"\n✅ 重建完成: {stats['total']} 社区, singleton {stats['singletons']}, "
              f"最大 {stats['max_size']}, 平均 {stats['avg_size']:.1f}")


if __name__ == '__main__':
    asyncio.run(main())
