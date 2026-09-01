#!/usr/bin/env python3
"""
视频内预合并原型验证（两跳消歧第一跳，#68 第 0 项）
用 Neo4j 里已有视频的 raw_extraction 实测合并效果：
- L1 精确分组（规范化哈希）
- L2 模糊配对 + 并查集传递闭包（熵门控防短名误并）
- 组内聚合（最完整名字/类型众数/描述收集/来源收集）
- 关系按组映射预合并
只读不写 Neo4j。

用法：python3 experiment/intra_video_merge_test.py [media_id ...]
"""
import json
import re
import sys
from difflib import SequenceMatcher


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # 路径压缩
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def normalize_exact(name: str) -> str:
    """L1：小写 + 压缩空白（对齐 Disambiguator._normalize_exact）"""
    return re.sub(r'\s+', ' ', name.lower()).strip()


def normalize_fuzzy(name: str) -> str:
    """L2：只保留字母数字中文（对齐 Disambiguator._normalize_fuzzy）"""
    return re.sub(r'[^a-z0-9一-鿿]+', '', name.lower())


def has_high_entropy(normalized: str) -> bool:
    """熵门控：长度>=6 或词数>=2（对齐 Disambiguator._has_high_entropy）"""
    return len(normalized) >= 6 or len(normalized.split()) >= 2


def load_raw_entities(media_id: int):
    """从 Neo4j 读某视频全部 raw_extraction，摊平实体和关系"""
    from app.clients.neo4j_client import Neo4jClient
    client = Neo4jClient()
    entities, relationships = [], []
    with client.driver.session() as s:
        r = s.run("""
            MATCH (seg:Segment {media_id: $mid})
            RETURN seg.segment_index AS idx, seg.raw_extraction AS raw
            ORDER BY idx
        """, mid=media_id)
        for row in r:
            data = json.loads(row['raw'])
            for e in data.get('entities', []):
                entities.append({
                    'name': e['name'], 'type': e.get('type', 'Other'),
                    'description': e.get('description', ''),
                    'segment_index': row['idx'],
                })
            for rel in data.get('relationships', []):
                relationships.append({
                    'source': rel['source'], 'target': rel['target'],
                    'description': rel.get('description', ''),
                    'strength': rel.get('strength', 5),
                    'segment_index': row['idx'],
                })
    client.close()
    return entities, relationships


def merge_entities(entities, l2_threshold=0.85):
    """L1 分组 + L2 并查集闭包 → 视频级实体组"""
    # L1：精确规范化分组
    l1_groups = {}
    for e in entities:
        key = normalize_exact(e['name'])
        l1_groups.setdefault(key, []).append(e)
    groups = list(l1_groups.values())
    group_names = [g[0]['name'] for g in groups]  # 组代表名（第一个）

    # L2：组间模糊配对（用模糊规范化后的名字），并查集闭包
    uf = UnionFind(len(groups))
    fuzzy_names = [normalize_fuzzy(n) for n in group_names]
    for i in range(len(groups)):
        if not has_high_entropy(fuzzy_names[i]):
            continue
        for j in range(i + 1, len(groups)):
            if uf.find(i) == uf.find(j):
                continue
            if not has_high_entropy(fuzzy_names[j]):
                continue
            score = SequenceMatcher(None, fuzzy_names[i], fuzzy_names[j]).ratio()
            if score >= l2_threshold:
                uf.union(i, j)

    # 闭包后的最终组
    merged = {}
    for i, g in enumerate(groups):
        merged.setdefault(uf.find(i), []).extend(g)

    # 组内聚合
    result = []
    for members in merged.values():
        names = [m['name'] for m in members]
        canonical_name = max(names, key=len)  # 最完整名字
        types = [m['type'] for m in members]
        canonical_type = max(set(types), key=types.count)  # 类型众数
        result.append({
            'name': canonical_name,
            'type': canonical_type,
            'surface_forms': sorted(set(names)),
            'descriptions': [m['description'] for m in members],
            'sources': sorted(set(m['segment_index'] for m in members)),
            'mention_count': len(members),
        })
    return result, uf, groups


def merge_relationships(relationships, merged_entities):
    """关系端点映射到视频级实体组，同组对预合并"""
    # 名字 → 组代表名 映射（含所有 surface forms）
    name2group = {}
    for g in merged_entities:
        for sf in g['surface_forms']:
            name2group[sf] = g['name']

    rel_groups = {}
    dropped = 0
    for rel in relationships:
        src = name2group.get(rel['source'])
        tgt = name2group.get(rel['target'])
        if not src or not tgt:
            dropped += 1
            continue
        rel_groups.setdefault((src, tgt), []).append(rel)

    merged_rels = []
    for (src, tgt), rels in rel_groups.items():
        merged_rels.append({
            'source': src, 'target': tgt,
            'descriptions': [r['description'] for r in rels],
            'strength': max(r['strength'] for r in rels),
            'source_count': len(rels),
            'sources': sorted(set(r['segment_index'] for r in rels)),
        })
    return merged_rels, dropped


def report(media_id):
    entities, relationships = load_raw_entities(media_id)
    merged_entities, uf, l1_groups = merge_entities(entities)
    merged_rels, dropped_rels = merge_relationships(relationships, merged_entities)

    print(f"\n{'='*60}")
    print(f"media_id={media_id}")
    print(f"实体：原始 {len(entities)} → L1 分组 {len(l1_groups)} → 最终 {len(merged_entities)}"
          f"（减少 {100*(1-len(merged_entities)/len(entities)):.0f}%）")
    print(f"关系：原始 {len(relationships)} → 最终 {len(merged_rels)}"
          f"（减少 {100*(1-len(merged_rels)/max(len(relationships),1)):.0f}%），端点未映射丢弃 {dropped_rels}")

    # 展示有多处来源的合并组（真正的重复）
    multi = [g for g in merged_entities if g['mention_count'] > 1]
    multi.sort(key=lambda g: -g['mention_count'])
    print(f"\n出现多次的实体 Top 8（共 {len(multi)} 个）：")
    for g in multi[:8]:
        forms = '/'.join(g['surface_forms'][:4])
        print(f"  ×{g['mention_count']} {g['name']}（{forms}）片段{g['sources'][:6]}")

    # L2 模糊合并的组（名字不完全相同的）
    fuzzy_merged = [g for g in merged_entities if len(g['surface_forms']) > 1]
    print(f"\nL2 模糊合并的组（共 {len(fuzzy_merged)} 个）：")
    for g in fuzzy_merged[:8]:
        print(f"  {' / '.join(g['surface_forms'][:4])}")


if __name__ == '__main__':
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    media_ids = [int(x) for x in sys.argv[1:]] or [7, 10, 12]
    for mid in media_ids:
        report(mid)
