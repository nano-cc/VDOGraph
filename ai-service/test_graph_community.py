#!/usr/bin/env python3
"""
图级社区检测：从 Neo4j 拉取所有实体和关系，跑 Leiden 层次化划分
验证多视频效果：每个社区跨了哪些视频
"""
import sys
sys.path.insert(0, '.')
from collections import defaultdict
from app.clients.neo4j_client import Neo4jClient
from app.services.community_detector import CommunityDetector

client = Neo4jClient()

with client.driver.session() as session:
    # 所有实体 + 来源片段（从 MENTIONED_IN 边重建 sources）
    entities = []
    for record in session.run("""
        MATCH (e:Entity)
        OPTIONAL MATCH (e)-[:MENTIONED_IN]->(s:Segment)
        RETURN e, collect(DISTINCT s.id) as segment_ids
    """):
        e = dict(record['e'])
        e.pop('name_embedding', None)
        e.pop('description_embedding', None)
        e['sources'] = [{'segment_id': sid} for sid in record['segment_ids'] if sid]
        entities.append(e)

    # 所有关系
    relationships = []
    for record in session.run("""
        MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
        RETURN s.id as source_id, s.name as source_name,
               t.id as target_id, t.name as target_name,
               r.description as description, r.strength as strength
    """):
        relationships.append({
            'source_entity_id': record['source_id'],
            'target_entity_id': record['target_id'],
            'source_entity_name': record['source_name'],
            'target_entity_name': record['target_name'],
            'description': record['description'],
            'strength': record['strength'] or 1
        })

client.close()
print(f"图规模: {len(entities)} 实体, {len(relationships)} 关系\n")

detector = CommunityDetector()
communities = detector._hierarchical_leiden(entities, relationships)

print(f"=== Leiden 层次化: {len(communities)} 个社区 ===\n")

# 每个社区跨哪些视频
def media_of(segment_ids):
    medias = set()
    for sid in segment_ids:
        # segment id 格式: media_100_segment_0
        parts = sid.split('_segment_')
        if parts:
            medias.add(parts[0])
    return sorted(medias)

for c in sorted(communities, key=lambda x: (x.level, -x.entity_count)):
    indent = '  ' * c.level
    parent = f' (父: {c.parent_id})' if c.parent_id else ''
    medias = media_of(c.source_segments)
    cross = f" 【跨视频: {', '.join(medias)}】" if len(medias) > 1 else f" [{medias[0]}]" if medias else ""
    names = [e.name for e in c.entities[:8]]
    suffix = f'... 等{c.entity_count}个' if c.entity_count > 8 else ''
    print(f"{indent}[L{c.level}] {c.id}{parent}: {c.entity_count} 实体{cross}")
    print(f"{indent}    {', '.join(names)}{suffix}")

# 统计跨视频社区数
cross_video = [c for c in communities if len(media_of(c.source_segments)) > 1]
print(f"\n跨视频社区: {len(cross_video)}/{len(communities)}")
