#!/usr/bin/env python3
"""
两跳消歧完整原型（#68 第 0 项）：第一跳视频内预合并（零 LLM）+ 第二跳批量 LLM 判重
用 Neo4j 已有视频的 raw_extraction 实测，只读不写。

第二跳对齐 Graphiti dedupe_nodes：
- 共享候选池（图级实体向量召回并集 + 视频内第一跳实体，编号 candidate_id）
- 批量 prompt：待判实体编号 id + 候选池 + 原文上下文 + few-shot（缩写/同名异物/同义词）
- 防御性解析：id 越界/重复/无效 candidate_id → 按新建处理；LLM 失效 → 全部新建

用法：python3 experiment/two_hop_disambiguation_test.py [media_id ...]
"""
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ==================== 第一跳：视频内预合并（零 LLM） ====================

class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))
    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def normalize_exact(name):
    return re.sub(r'\s+', ' ', name.lower()).strip()

def normalize_fuzzy(name):
    return re.sub(r'[^a-z0-9一-鿿]+', '', name.lower())

def has_high_entropy(normalized):
    return len(normalized) >= 6 or len(normalized.split()) >= 2


def l2_mergeable(a: str, b: str) -> bool:
    """保守护栏（实测修正版）：
    ① 包含关系（短名是长名子串）→ 合并
    ② 相似度 ≥0.93 且差异字符 ≥2 → 合并
    单字差异（养牛/养猪、CAT/RAT MEAT 类）不本地合并，留给第二跳 LLM
    """
    fa, fb = normalize_fuzzy(a), normalize_fuzzy(b)
    if not fa or not fb:
        return False
    if not (has_high_entropy(fa) and has_high_entropy(fb)):
        return False
    if fa in fb or fb in fa:
        return True
    score = SequenceMatcher(None, fa, fb).ratio()
    if score < 0.93:
        return False
    # 差异字符数（用对齐后的最小编辑距离近似：长度差 + 替换数）
    diff = abs(len(fa) - len(fb))
    if fa != fb and len(fa) == len(fb):
        diff = sum(1 for x, y in zip(fa, fb) if x != y)
    return diff >= 2


def hop1_merge(entities):
    """视频内预合并：L1 精确分组 + L2 保守模糊并查集"""
    l1_groups = defaultdict(list)
    for e in entities:
        l1_groups[normalize_exact(e['name'])].append(e)
    groups = list(l1_groups.values())
    rep_names = [max((m['name'] for m in g), key=len) for g in groups]

    uf = UnionFind(len(groups))
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if uf.find(i) == uf.find(j):
                continue
            if l2_mergeable(rep_names[i], rep_names[j]):
                uf.union(i, j)

    merged = defaultdict(list)
    for i, g in enumerate(groups):
        merged[uf.find(i)].extend(g)

    video_entities = []
    for members in merged.values():
        names = [m['name'] for m in members]
        types = [m['type'] for m in members]
        video_entities.append({
            'name': max(names, key=len),
            'type': max(set(types), key=types.count),
            'surface_forms': sorted(set(names)),
            'description': max((m['description'] for m in members), key=len),
            'sources': sorted(set(m['segment_index'] for m in members)),
            'mention_count': len(members),
        })
    return video_entities


# ==================== 第二跳：批量 LLM 判重（Graphiti 式） ====================

FEW_SHOT = """
<EXAMPLE>
ENTITY: "毛主席" (Person)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "毛泽东", "entity_types": ["Person"], "summary": "中华人民共和国主要创立者"}]
Result: duplicate_candidate_id = 0 （同一人物的不同称呼）

ENTITY: "IPHONE17" (Product)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "iPhone 17", "entity_types": ["Product"], "summary": "苹果2025年旗舰手机"}]
Result: duplicate_candidate_id = 0 （同一产品的不同写法）

ENTITY: "Java" (Concept，编程语言)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Java", "entity_types": ["Location"], "summary": "印度尼西亚的岛屿"}]
Result: duplicate_candidate_id = -1 （同名但指向完全不同的东西）

ENTITY: "养牛上市公司" (Organization)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "养猪上市公司", "entity_types": ["Organization"], "summary": "生猪养殖企业"}]
Result: duplicate_candidate_id = -1 （一字之差但业务完全不同的两家公司）

ENTITY: "巴黎的无产阶级" (Concept)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "巴黎无产阶级", "entity_types": ["Concept"], "summary": "巴黎工人阶级"}]
Result: duplicate_candidate_id = 0 （助词差异，同一概念）
</EXAMPLE>
"""

BATCH_SIZE = 20  # 每批待判实体数（防 prompt 爆 token）


def build_prompt(batch_entities, candidates, context_text):
    entities_json = json.dumps([
        {'id': i, 'name': e['name'], 'entity_types': [e['type']],
         'summary': e['description'][:120], 'surface_forms': e['surface_forms'][:4]}
        for i, e in enumerate(batch_entities)
    ], ensure_ascii=False)
    candidates_json = json.dumps([
        {'candidate_id': i, 'name': c['name'], 'entity_types': [c.get('type', 'Other')],
         'summary': (c.get('description') or '')[:120]}
        for i, c in enumerate(candidates)
    ], ensure_ascii=False)

    return f"""You are an entity deduplication assistant.
NEVER fabricate entity names or mark distinct entities as duplicates.

<VIDEO CONTEXT>
{context_text[:800]}
</VIDEO CONTEXT>

<ENTITIES>
{entities_json}
</ENTITIES>

<EXISTING ENTITIES>
{candidates_json}
</EXISTING ENTITIES>

Each of the above ENTITIES was extracted from a video's transcript. For each entity, determine if it is a duplicate of any EXISTING ENTITY.
Entities should only be considered duplicates if they refer to the *same real-world object or concept*.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They differ by a single character that changes the meaning (e.g. 养牛 vs 养猪, CAT MEAT vs RAT MEAT).
- They have similar names or purposes but refer to separate instances or concepts.

Task:
ENTITIES contains {len(batch_entities)} entities with IDs 0 through {len(batch_entities) - 1}.
Your response MUST include EXACTLY {len(batch_entities)} resolutions with IDs 0 through {len(batch_entities) - 1}. Do not skip or add IDs.

For every entity, provide:
- `id`: integer id from ENTITIES
- `name`: the best full name for the entity (preserve the original name unless a duplicate has a more complete name)
- `duplicate_candidate_id`: the `candidate_id` of the EXISTING ENTITY that is the best duplicate match, or -1 if there is no duplicate
{FEW_SHOT}
Return ONLY a JSON object:
{{"entity_resolutions": [{{"id": 0, "name": "...", "duplicate_candidate_id": 0}}, ...]}}
"""


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def hop2_batch_judge(video_entities, group_id, context_text, log=print):
    """批量 LLM 判重：候选池 = 图级实体召回并集 + 视频内第一跳实体"""
    from app.clients.embedding import EmbeddingClient
    from app.clients.deepseek import DeepSeekClient
    from app.clients.neo4j_client import Neo4jClient

    embedding_client = EmbeddingClient()
    deepseek = DeepSeekClient()
    neo4j = Neo4jClient()

    # 1. 全部第一跳实体算 embedding（批量，异步方法）
    names = [e['name'] for e in video_entities]
    embeddings = await embedding_client.embed_batch(names)

    # 2. 每个实体向量召回图级候选（Neo4j 精确余弦，group 闭包）
    candidate_map = {}
    for e, emb in zip(video_entities, embeddings):
        if not emb:
            continue
        try:
            results = neo4j.find_similar_entities(emb, group_id, top_k=5, threshold=0.5)
            for item in results:
                ent = item['entity']
                if ent['id'] not in candidate_map:
                    candidate_map[ent['id']] = ent
        except Exception as ex:
            log(f"  召回失败 {e['name']}: {ex}")
    graph_candidates = list(candidate_map.values())

    # 3. 候选池 = 图级候选 + 视频内第一跳实体（覆盖同视频语义重复，如 毛主席/毛泽东）
    candidates = graph_candidates + [
        {'id': f'video_{i}', 'name': e['name'], 'type': e['type'], 'description': e['description']}
        for i, e in enumerate(video_entities)
    ]
    log(f"  候选池：图级 {len(graph_candidates)} + 视频内 {len(video_entities)} = {len(candidates)}")

    # 4. 分批 LLM 判重
    llm_calls = 0
    resolutions = {}  # video_entity_idx -> (candidate_idx or -1, chosen_name)
    for start in range(0, len(video_entities), BATCH_SIZE):
        batch = video_entities[start:start + BATCH_SIZE]
        prompt = build_prompt(batch, candidates, context_text)
        try:
            response = await deepseek.acall_llm(prompt)
            llm_calls += 1
            if '```json' in response:
                response = response.split('```json')[1].split('```')[0].strip()
            elif '```' in response:
                response = response.split('```')[1].split('```')[0].strip()
            data = json.loads(response)
            for r in data.get('entity_resolutions', []):
                rid = r.get('id', -1)
                if not (0 <= rid < len(batch)):
                    continue  # 越界防呆
                if start + rid in resolutions:
                    continue  # 重复防呆
                cid = r.get('duplicate_candidate_id', -1)
                if cid != -1 and not (0 <= cid < len(candidates)):
                    cid = -1  # 无效候选防呆
                resolutions[start + rid] = (cid, r.get('name') or batch[rid]['name'])
        except Exception as ex:
            log(f"  LLM 批次失败（{start}起），降级全部新建: {ex}")
    neo4j.close()

    # 5. 默认未判的按新建（漏判防呆）
    for i, e in enumerate(video_entities):
        resolutions.setdefault(i, (-1, e['name']))
    return resolutions, candidates, llm_calls


async def run_test(media_id):
    from app.clients.neo4j_client import Neo4jClient

    # 读 raw_extraction
    neo4j = Neo4jClient()
    entities = []
    transcript_head = ""
    with neo4j.driver.session() as s:
        r = s.run("""
            MATCH (seg:Segment {media_id: $mid})
            RETURN seg.segment_index AS idx, seg.raw_extraction AS raw, seg.transcript AS t
            ORDER BY idx
        """, mid=media_id)
        for row in r:
            data = json.loads(row['raw'])
            for e in data.get('entities', []):
                entities.append({'name': e['name'], 'type': e.get('type', 'Other'),
                                 'description': e.get('description', ''), 'segment_index': row['idx']})
            if row['idx'] == 0:
                transcript_head = (row['t'] or '')[:800]
    neo4j.close()

    print(f"\n{'='*64}\nmedia_id={media_id}  原始实体 {len(entities)}")

    # 第一跳
    video_entities = hop1_merge(entities)
    print(f"第一跳（零 LLM）：{len(entities)} → {len(video_entities)}"
          f"（减少 {100*(1-len(video_entities)/len(entities)):.0f}%）")

    # 第二跳
    # 注：media_id 对应 group user_2；排除本视频自己的实体需要图级已有实体——
    # 这些视频已建图，图里已有它们的实体，正好测"重复构建时的幂等合并"
    resolutions, candidates, llm_calls = await hop2_batch_judge(
        video_entities, "user_2", transcript_head)

    # 统计判决
    merged_graph, merged_video, new_entities = 0, 0, 0
    examples = []
    for i, (cid, chosen) in sorted(resolutions.items()):
        e = video_entities[i]
        if cid == -1:
            new_entities += 1
        else:
            target = candidates[cid]
            is_video = str(target['id']).startswith('video_')
            if is_video:
                merged_video += 1
            else:
                merged_graph += 1
            if len(examples) < 12:
                mark = '视频内' if is_video else '图级'
                same = '' if e['name'] == target['name'] else '（改名）'
                examples.append(f"    {e['name']} → {target['name']}[{mark}]{same}")

    print(f"第二跳：LLM 调用 {llm_calls} 次（每实体一次需 {len(video_entities)} 次，"
          f"省 {100*(1-llm_calls/max(len(video_entities),1)):.0f}%）")
    print(f"  判决：并入图级 {merged_graph}，视频内互并 {merged_video}，新建 {new_entities}")
    print(f"  判决样例：")
    for ex in examples:
        print(ex)


async def main(media_ids):
    for m in media_ids:
        await run_test(m)


if __name__ == '__main__':
    media_ids = [int(x) for x in sys.argv[1:]] or [7]
    asyncio.run(main(media_ids))
