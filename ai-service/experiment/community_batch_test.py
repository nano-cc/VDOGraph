"""#71 批量化社区归属基准测试：
1. 幂等性：对已分配实体跑 assign_entities → 应全部 skipped
2. 性能：摘掉指定 media 实体的 BELONGS_TO 边后重跑 → 对比旧版 ~11s/实体
注意：会真实改写社区归属（重跑后状态等价，摘要可能略有漂移）
"""
import asyncio
import sys
import time

sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')

MEDIA_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 41
GROUP_ID = 'user_2'


async def main():
    from app.services.community_updater import CommunityUpdater
    from app.clients.neo4j_client import Neo4jClient

    neo = Neo4jClient()
    updater = CommunityUpdater()

    # 取出该 media 消歧后的实体（id/name/type/description）
    with neo.driver.session() as s:
        rows = s.run("""
            MATCH (m:Media {id: $mid})-[:HAS_SEGMENT]->(:Segment)<-[:MENTIONED_IN]-(e:Entity)
            RETURN DISTINCT e.id AS id, e.name AS name, e.type AS type, e.description AS description
        """, {'mid': f'media_{MEDIA_ID}'})
        entities = [dict(r) for r in rows]
    print(f'entities of media {MEDIA_ID}: {len(entities)}', flush=True)
    if not entities:
        return

    progress_marks = []

    async def progress(done, total):
        progress_marks.append((time.time(), done, total))
        print(f'[PROGRESS] {done}/{total}', flush=True)

    # --- 1. 幂等性：已分配的应全部跳过 ---
    t0 = time.time()
    stats = await updater.assign_entities(entities, GROUP_ID, progress_cb=progress)
    print(f'[IDEMPOTENT] {time.time()-t0:.1f}s stats={stats}', flush=True)
    assert stats['skipped'] == len(entities) or stats['assigned'] + stats['created'] == 0, \
        f'幂等性异常: {stats}'

    # --- 2. 摘掉 BELONGS_TO 重跑（性能基准） ---
    with neo.driver.session() as s:
        r = s.run("""
            MATCH (e:Entity)-[b:BELONGS_TO]->(:Community)
            WHERE e.id IN $ids
            DELETE b
            RETURN count(b) AS n
        """, {'ids': [e['id'] for e in entities]})
        print(f'[DETACH] removed {r.single()["n"]} BELONGS_TO edges', flush=True)

    t0 = time.time()
    stats = await updater.assign_entities(entities, GROUP_ID, progress_cb=progress)
    elapsed = time.time() - t0
    print(f'\n[BATCHED] {elapsed:.1f}s for {len(entities)} entities '
          f'({elapsed/len(entities):.2f}s/entity, 旧版 ~11s/entity) stats={stats}', flush=True)

    # 验证：全部实体重新有社区
    with neo.driver.session() as s:
        r = s.run("""
            MATCH (e:Entity) WHERE e.id IN $ids
            OPTIONAL MATCH (e)-[:BELONGS_TO]->(c:Community)
            RETURN count(e) AS total, count(c) AS assigned
        """, {'ids': [e['id'] for e in entities]})
        row = r.single()
        print(f'[VERIFY] {row["assigned"]}/{row["total"]} entities re-assigned', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
