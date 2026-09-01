"""
第二跳：图级批量消歧（docs/kg-phase4-commit-optimization.md §4/§5）
对齐 Graphiti resolve_extracted_nodes：对图 L1/L2 免费级联 + 批量 LLM 判重 + 防御性后处理。

四层漏斗位置：
  第一跳（intra_video_merger）视频内 L1/L2 已合并 → 本模块接手：
  第三层 对图 L1/L2（带全组变体匹配，免费）
  第四层 批量 LLM（按实体挂 Top-K 候选 + few-shot + 防御解析 + 链式闭环）

设计要点：
- 每个待判实体只带自己的候选（图级向量召回 + 视频内相似 + 边界对对端），不用共享大池子
- LLM 失效/输出异常一律降级为"新建"（错放过是软错误，错合并是硬错误）
- 链式决议（A→视频内 B，B→图级 C）用并查集闭包，A 直达 C
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.logging import logger
from app.services.intra_video_merger import (
    VideoEntityGroup,
    normalize_exact, normalize_fuzzy, has_high_entropy, shingles, jaccard,
)

# ---- 阈值 ----
_GRAPH_L2_JACCARD = 0.9          # 对图 L2 直接并入阈值（对齐 Graphiti）
_VIDEO_CANDIDATE_COSINE = 0.5    # 视频内相似候选阈值
_GRAPH_RECALL_TOP_K = 5          # 图级向量召回每实体 Top-K
_GRAPH_RECALL_THRESHOLD = 0.5    # 图级召回相似度阈值
_BATCH_SIZE = 20                 # 每批待判实体数（防 prompt 爆 token）

_ACTION_MERGE = 'merge'          # 并入图级已有实体
_ACTION_CREATE = 'create'        # 新建


@dataclass
class EntityResolution:
    """单个视频级实体组的最终决议"""
    group: VideoEntityGroup
    action: str                    # merge / create
    target_entity_id: str          # merge: 图级实体 id；create: 新建确定性 id
    chosen_name: str               # LLM 选的更完整标准名（或组代表名）
    via: str                       # graph_l1 / graph_l2 / llm / llm_video_chain / new


@dataclass
class ResolutionPlan:
    """prepare_commit 的产出之一：完整决议表"""
    resolutions: List[EntityResolution]
    stats: Dict = field(default_factory=dict)


class BatchDisambiguator:
    def __init__(self, group_id: str):
        from app.clients.deepseek import DeepSeekClient
        from app.clients.embedding import EmbeddingClient
        self.group_id = group_id
        self.deepseek_client = DeepSeekClient()
        self.embedding_client = EmbeddingClient()
        try:
            from app.clients.neo4j_client import Neo4jClient
            self.neo4j_client = Neo4jClient()
        except Exception as e:
            logger.warning(f"[BATCH_DISAMBIG] Neo4j unavailable: {e}")
            self.neo4j_client = None

    # ==================== 主入口 ====================

    async def resolve(self, video_groups: List[VideoEntityGroup],
                      borderline_pairs: List[Tuple[str, str]],
                      context_text: str) -> ResolutionPlan:
        start = time.time()
        stats = {'total_groups': len(video_groups), 'graph_l1': 0, 'graph_l2': 0,
                 'llm_merge_graph': 0, 'llm_merge_video': 0, 'new': 0, 'llm_calls': 0}

        # ---- 第三层：对图 L1/L2 ----
        graph_entities = await self._load_graph_entities()
        resolutions: Dict[int, EntityResolution] = {}
        unresolved: List[int] = []
        for i, g in enumerate(video_groups):
            hit, via = self._match_graph(g, graph_entities)
            if hit:
                resolutions[i] = EntityResolution(g, _ACTION_MERGE, hit['id'], hit['name'], via)
                stats[via] += 1
            else:
                unresolved.append(i)
        logger.info(f"[BATCH_DISAMBIG] 对图 L1/L2：{len(resolutions)} 直接并入，{len(unresolved)} 待 LLM")

        # ---- 第四层：批量 LLM ----
        if unresolved:
            llm_resolutions, calls = await self._llm_judge_batch(
                video_groups, unresolved, graph_entities, borderline_pairs, context_text, resolutions)
            stats['llm_calls'] = calls
            for i, res in llm_resolutions.items():
                resolutions[i] = res
                if res.action == _ACTION_MERGE:
                    stats['llm_merge_graph'] += 1
                elif res.action == 'merge_video':
                    stats['llm_merge_video'] += 1
                else:
                    stats['new'] += 1

        # ---- 链式闭环：视频内互并的组挂到最终图级实体 ----
        self._close_chains(resolutions)

        # 未覆盖的组（理论不会到这，兜底）→ 新建
        for i, g in enumerate(video_groups):
            if i not in resolutions:
                resolutions[i] = self._new_resolution(g, 'new')

        plan = ResolutionPlan(
            resolutions=[resolutions[i] for i in sorted(resolutions)],
            stats={**stats, 'duration_ms': (time.time() - start) * 1000})
        logger.info(f"[BATCH_DISAMBIG] 完成：{plan.stats}")
        return plan

    # ==================== 第三层：对图 L1/L2 ====================

    async def _load_graph_entities(self) -> List[Dict]:
        if not self.neo4j_client:
            return []
        try:
            return await asyncio.to_thread(self.neo4j_client.get_group_entities, self.group_id)
        except Exception as e:
            logger.warning(f"[BATCH_DISAMBIG] 加载图实体失败，降级为全新建: {e}")
            return []

    def _match_graph(self, group: VideoEntityGroup, graph_entities: List[Dict]
                     ) -> Tuple[Optional[Dict], Optional[str]]:
        """带全组变体对图匹配：L1 精确（含 aliases）→ L2 3-gram Jaccard"""
        # L1：任一 surface_form 规范化后命中图的 name 或 aliases
        for sf in group.surface_forms:
            key = normalize_exact(sf)
            for ge in graph_entities:
                if normalize_exact(ge['name'] or '') == key:
                    return ge, 'graph_l1'
                for alias in (ge.get('aliases') or []):
                    if normalize_exact(alias) == key:
                        return ge, 'graph_l1'

        # L2：全组变体 shingle vs 图实体 shingle，取最高分
        if not has_high_entropy(normalize_fuzzy(group.name)):
            return None, None
        group_shingles = [shingles(normalize_fuzzy(sf)) for sf in group.surface_forms]
        best, best_score = None, 0.0
        for ge in graph_entities:
            ge_names = [ge['name']] + list(ge.get('aliases') or [])
            ge_shingles = [shingles(normalize_fuzzy(n)) for n in ge_names if n]
            score = max((jaccard(gs, ges) for gs in group_shingles for ges in ge_shingles),
                        default=0.0)
            if score > best_score:
                best, best_score = ge, score
        if best is not None and best_score >= _GRAPH_L2_JACCARD:
            return best, 'graph_l2'
        return None, None

    # ==================== 第四层：批量 LLM 判重 ====================

    async def _llm_judge_batch(self, video_groups, unresolved, graph_entities,
                               borderline_pairs, context_text, existing_resolutions
                               ) -> Tuple[Dict[int, EntityResolution], int]:
        # 1. 批量 embedding（一次调用）
        names = [video_groups[i].name for i in unresolved]
        try:
            embeddings = await self.embedding_client.embed_batch(names)
        except Exception as e:
            logger.error(f"[BATCH_DISAMBIG] 批量 embedding 失败，全部降级新建: {e}")
            return {i: self._new_resolution(video_groups[i], 'new') for i in unresolved}, 0
        emb_map = dict(zip(unresolved, embeddings))

        # 2. 每实体候选：图级向量召回 + 视频内相似 + 边界对对端
        borderline_map = {}
        for a, b in borderline_pairs:
            borderline_map.setdefault(normalize_exact(a), set()).add(normalize_exact(b))
            borderline_map.setdefault(normalize_exact(b), set()).add(normalize_exact(a))

        name2idx = {}
        for i, g in enumerate(video_groups):
            for sf in g.surface_forms:
                name2idx[normalize_exact(sf)] = i

        candidates_map: Dict[int, List[Dict]] = {}
        for i in unresolved:
            cands = []
            emb = emb_map.get(i)
            # 图级向量召回
            if emb and self.neo4j_client:
                try:
                    recalls = await asyncio.to_thread(
                        self.neo4j_client.find_similar_entities,
                        emb, self.group_id, _GRAPH_RECALL_TOP_K, _GRAPH_RECALL_THRESHOLD)
                    for item in recalls:
                        ent = dict(item['entity'])
                        ent['_origin'] = 'graph'
                        cands.append(ent)
                except Exception as e:
                    logger.warning(f"[BATCH_DISAMBIG] 图级召回失败 {video_groups[i].name}: {e}")
            # 视频内相似（余弦）
            if emb:
                for j, g in enumerate(video_groups):
                    if j == i:
                        continue
                    other = emb_map.get(j) if j in emb_map else None
                    if other is None and j in existing_resolutions:
                        continue  # 已并入图的不当视频内候选（决议已指向图）
                    if other is None:
                        continue
                    score = _cosine(emb, other)
                    if score >= _VIDEO_CANDIDATE_COSINE:
                        cands.append({'id': f'video_{j}', 'name': g.name, 'type': g.type,
                                      'description': g.descriptions[0] if g.descriptions else '',
                                      '_origin': 'video'})
            # 边界对对端强制进候选
            for sf in video_groups[i].surface_forms:
                for other_key in borderline_map.get(normalize_exact(sf), ()):
                    j = name2idx.get(other_key)
                    if j is not None and j != i:
                        g = video_groups[j]
                        cand = {'id': f'video_{j}', 'name': g.name, 'type': g.type,
                                'description': g.descriptions[0] if g.descriptions else '',
                                '_origin': 'video', '_borderline': True}
                        if not any(c.get('id') == cand['id'] for c in cands):
                            cands.append(cand)
            candidates_map[i] = cands

        # 3. 分批 LLM 判重
        resolutions: Dict[int, EntityResolution] = {}
        llm_calls = 0
        for start in range(0, len(unresolved), _BATCH_SIZE):
            batch_indices = unresolved[start:start + _BATCH_SIZE]
            batch_result = await self._judge_one_batch(
                video_groups, batch_indices, candidates_map, context_text)
            llm_calls += 1
            resolutions.update(batch_result)
        return resolutions, llm_calls

    async def _judge_one_batch(self, video_groups, batch_indices, candidates_map, context_text
                               ) -> Dict[int, EntityResolution]:
        """单批 LLM 判重 + 防御性解析"""
        prompt = self._build_prompt(video_groups, batch_indices, candidates_map, context_text)
        try:
            response = await self.deepseek_client.acall_llm(prompt)
            data = self._parse_json(response)
            items = data.get('entity_resolutions', [])
        except Exception as e:
            logger.error(f"[BATCH_DISAMBIG] LLM 批次失败，本批 {len(batch_indices)} 个降级新建: {e}")
            return {i: self._new_resolution(video_groups[i], 'new') for i in batch_indices}

        result = {}
        seen_local = set()
        for item in items:
            local_id = item.get('id', -1)
            if not (0 <= local_id < len(batch_indices)) or local_id in seen_local:
                continue  # 越界/重复防呆
            seen_local.add(local_id)
            i = batch_indices[local_id]
            g = video_groups[i]
            cands = candidates_map.get(i, [])
            cid = item.get('duplicate_candidate_id', -1)
            chosen = item.get('name') or g.name
            if cid == -1 or not (0 <= cid < len(cands)):
                result[i] = self._new_resolution(g, 'new', chosen)
                continue
            target = cands[cid]
            if target.get('_origin') == 'video':
                # 视频内互并：记 merge_video，target_entity_id 暂存 video_{idx}，闭环时解析
                result[i] = EntityResolution(g, 'merge_video', target['id'], chosen, 'llm_video_chain')
            else:
                result[i] = EntityResolution(g, _ACTION_MERGE, target['id'], chosen or target['name'], 'llm')
        # 漏判防呆
        for local_id, i in enumerate(batch_indices):
            if i not in result:
                result[i] = self._new_resolution(video_groups[i], 'new')
        return result

    def _build_prompt(self, video_groups, batch_indices, candidates_map, context_text) -> str:
        lines = []
        for local_id, i in enumerate(batch_indices):
            g = video_groups[i]
            cands = candidates_map.get(i, [])
            lines.append(f'ENTITY id={local_id}: "{g.name}" ({g.type})')
            lines.append(f'  surface_forms: {json.dumps(g.surface_forms[:6], ensure_ascii=False)}')
            if g.descriptions:
                lines.append(f'  summary: {g.descriptions[0][:120]}')
            lines.append('  CANDIDATES:')
            for cid, c in enumerate(cands):
                origin = '图谱已有' if c.get('_origin') == 'graph' else '本视频'
                desc = (c.get('description') or '')[:120]
                lines.append(f'    [{cid}] "{c.get("name")}" ({c.get("type", "Other")}, {origin}) — {desc}')
            if not cands:
                lines.append('    （无候选）')
            lines.append('')
        entities_block = '\n'.join(lines)

        return f"""You are an entity deduplication assistant for a knowledge graph built from video transcripts (ASR/OCR, may contain transcription errors).
NEVER fabricate entity names or mark distinct entities as duplicates.

<VIDEO CONTEXT>
{context_text[:800]}
</VIDEO CONTEXT>

{entities_block}

For each ENTITY, determine if it is a duplicate of any of ITS OWN CANDIDATES.
Entities should only be considered duplicates if they refer to the *same real-world object or concept*.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They differ by a single character that changes the meaning (e.g. 养牛上市公司 vs 养猪上市公司, CAT MEAT vs RAT MEAT).
- They have similar names but refer to separate instances or concepts (e.g. iPhone 17 vs iPhone 17 Pro, VIVO X300 vs VIVO X300S).
DO mark as duplicates:
- ASR/transcription variants of the same thing (e.g. 比斯麦 vs 俾斯麦, 真我GT8PRO vs 真果GD8PRO).
- Abbreviations / different scripts of the same thing (e.g. NYC vs New York City, 毛主席 vs 毛泽东).

Task:
There are {len(batch_indices)} entities with IDs 0 through {len(batch_indices) - 1}.
Your response MUST include EXACTLY {len(batch_indices)} resolutions with IDs 0 through {len(batch_indices) - 1}. Do not skip or add IDs.

For every entity, provide:
- `id`: integer id from ENTITY
- `name`: the best full name (preserve original unless a duplicate candidate has a more complete name)
- `duplicate_candidate_id`: the candidate's [index] if duplicate, or -1 if none

Return ONLY a JSON object:
{{"entity_resolutions": [{{"id": 0, "name": "...", "duplicate_candidate_id": -1}}]}}
"""

    @staticmethod
    def _parse_json(response: str) -> dict:
        if '```json' in response:
            response = response.split('```json')[1].split('```')[0].strip()
        elif '```' in response:
            response = response.split('```')[1].split('```')[0].strip()
        return json.loads(response)

    # ==================== 链式闭环 ====================

    def _close_chains(self, resolutions: Dict[int, EntityResolution]):
        """视频内互并的链式决议闭包：A→视频内B，B→图级C，则 A 直达 C"""
        by_idx = dict(resolutions)
        for i, r in list(by_idx.items()):
            if r.action != 'merge_video':
                continue
            seen = {i}
            cur = r
            final = None
            while cur.action == 'merge_video':
                try:
                    j = int(cur.target_entity_id.split('_', 1)[1])
                except (IndexError, ValueError):
                    break
                if j in seen or j not in by_idx:
                    break  # 成环或断裂
                seen.add(j)
                nxt = by_idx[j]
                if nxt.action == 'merge_video':
                    cur = nxt
                    continue
                final = nxt  # 挂到终点（图级 merge 或 create）
                break
            if final is not None:
                r.action = final.action
                r.target_entity_id = final.target_entity_id
                r.chosen_name = final.chosen_name
            else:
                # 链成环/断裂：降级为新建（安全方向）
                r.action = _ACTION_CREATE
                r.target_entity_id = _new_entity_id(self.group_id, r.chosen_name)

    # ==================== 工具 ====================

    def _new_resolution(self, group: VideoEntityGroup, via: str, chosen_name: str = None) -> EntityResolution:
        name = chosen_name or group.name
        return EntityResolution(group, _ACTION_CREATE, _new_entity_id(self.group_id, name), name, via)


def _new_entity_id(group_id: str, name: str) -> str:
    """新建实体确定性 id（与旧 Disambiguator._create_canonical_entity 同规则）"""
    return f"entity_{group_id}_{normalize_fuzzy(name)}"


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
