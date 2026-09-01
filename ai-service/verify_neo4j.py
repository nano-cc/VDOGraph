#!/usr/bin/env python3
"""
验证 Neo4j 保存结果
1. 节点/边数量
2. 溯源链路（Entity -> Segment -> Media）
3. embedding 是否都保存了
"""
from app.clients.neo4j_client import Neo4jClient

client = Neo4jClient()

with client.driver.session() as session:
    print("=== 1. 节点数量 ===")
    for label in ['Media', 'Segment', 'Entity', 'Community']:
        count = session.run(f"MATCH (n:{label}) RETURN count(n) as c").single()['c']
        print(f"  {label}: {count}")

    print("\n=== 2. 边数量 ===")
    for rel_type in ['HAS_SEGMENT', 'MENTIONED_IN', 'RELATES_TO', 'BELONGS_TO', 'CONTAINS']:
        count = session.run(f"MATCH ()-[r:{rel_type}]->() RETURN count(r) as c").single()['c']
        print(f"  {rel_type}: {count}")

    print("\n=== 3. Embedding 覆盖检查 ===")
    checks = [
        ("Media.title_embedding", "MATCH (m:Media) RETURN count(m) as total, count(m.title_embedding) as with_emb"),
        ("Segment.transcript_embedding", "MATCH (s:Segment) RETURN count(s) as total, count(s.transcript_embedding) as with_emb"),
        ("Entity.name_embedding", "MATCH (e:Entity) RETURN count(e) as total, count(e.name_embedding) as with_emb"),
        ("Entity.description_embedding", "MATCH (e:Entity) RETURN count(e) as total, count(e.description_embedding) as with_emb"),
        ("RELATES_TO.description_embedding", "MATCH ()-[r:RELATES_TO]->() RETURN count(r) as total, count(r.description_embedding) as with_emb"),
        ("Community.summary_embedding", "MATCH (c:Community) RETURN count(c) as total, count(c.summary_embedding) as with_emb"),
    ]
    for name, query in checks:
        row = session.run(query).single()
        status = "✅" if row['total'] == row['with_emb'] else "❌"
        print(f"  {status} {name}: {row['with_emb']}/{row['total']}")

    print("\n=== 4. 溯源链路抽查（随机一个实体）===")
    result = session.run("""
        MATCH (e:Entity)-[:MENTIONED_IN]->(s:Segment)<-[:HAS_SEGMENT]-(m:Media)
        RETURN e.name as entity, s.id as segment, s.start_ms as start_ms, s.end_ms as end_ms, m.title as media
        LIMIT 3
    """)
    rows = list(result)
    if rows:
        for row in rows:
            print(f"  ✅ 实体「{row['entity']}」 -> 片段 {row['segment']} ({row['start_ms']}ms-{row['end_ms']}ms) -> 视频「{row['media']}」")
    else:
        print("  ❌ 没有找到完整的 Entity->Segment->Media 链路")

    print("\n=== 5. 关系溯源抽查（RELATES_TO 边的 source_segment_ids）===")
    result = session.run("""
        MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
        RETURN s.name as source, t.name as target, r.description as desc, r.source_segment_ids as segments
        LIMIT 3
    """)
    for row in result:
        print(f"  {row['source']} -> {row['target']}: {row['desc'][:40]}...")
        print(f"    来源片段: {row['segments']}")

    print("\n=== 6. 社区溯源抽查 ===")
    result = session.run("""
        MATCH (c:Community)-[:CONTAINS]->(s:Segment)
        RETURN c.id as community, c.entity_count as entities, collect(s.id) as segments
        LIMIT 3
    """)
    for row in result:
        print(f"  社区 {row['community']} ({row['entities']} 实体) -> 片段: {row['segments']}")

client.close()
print("\n=== 验证完成 ===")
