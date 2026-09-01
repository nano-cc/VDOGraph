"""
社区增量更新服务（Graphiti 式，#68 增强，#71 批量化）
新实体入库后按三层兜底链分配社区：
1. 邻居众数投票（零 LLM，对齐 Graphiti determine_entity_community）：
   刚写入的 RELATES_TO 邻居所属社区的众数，票数 ≥ NEIGHBOR_VOTE_MIN_VOTES 直接加入。
   批量化：一次 Cypher 拿回所有实体的投票明细，众数在内存里算；跑 2 轮，
   第 2 轮吃到第 1 轮新挂的 BELONGS_TO 边（保留串行版的链式受益）
2. 向量召回候选社区 + 批量 LLM 判断归属（10 实体/次调用，对齐 batch_disambiguator 手法）
3. 创建单实体社区（摘要直接用实体描述，防幻觉，确定性 id；批量 embed + UNWIND 落库）
加入社区的摘要归并按社区分组：同社区多个实体一次 LLM 归并（而非逐实体串行归并）。
已有所属社区的实体跳过（一次 IN 查询批量预过滤，天然幂等）
长期偏移后可用 Leiden 全量重跑纠偏（/community/detect + rebuild）
"""
import hashlib
import json
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from app.clients.neo4j_client import Neo4jClient
from app.clients.embedding import EmbeddingClient
from app.clients.deepseek import DeepSeekClient
from app.core.logging import logger

# 邻居投票最少票数：只有 1 个邻居带社区时归属证据太弱，交下一层（LLM）判
NEIGHBOR_VOTE_MIN_VOTES = 2

# 批量 LLM 判归属每批实体数（对齐 batch_disambiguator 的经验值）
LLM_JUDGE_BATCH_SIZE = 10


JUDGE_PROMPT = """你是知识图谱社区归属判断助手。

新实体:
{entity_name} ({entity_type})
描述: {entity_description}

候选社区（已有主题簇）:
{candidates}

任务: 判断新实体在主题上是否属于某个候选社区。
- 只有主题确实相关才返回对应序号
- 主题不符或不确定时返回 -1（宁缺毋滥，避免污染社区）

只返回 JSON:
{{"community_index": 0 或 1 或 2 或 -1, "reason": "简要理由"}}
"""

JUDGE_BATCH_PROMPT = """你是知识图谱社区归属判断助手。

下面是 {n} 个新实体，每个自带候选社区列表。逐个判断实体在主题上是否属于它的某个候选社区。

{blocks}

判断规则（对每个实体独立适用）:
- 只有主题确实相关才选对应候选序号（该实体的候选从 0 开始编号）
- 主题不符或不确定时选 -1（宁缺毋滥，避免污染社区）

只返回 JSON 数组，与实体编号一一对应:
[{{"entity": 1, "community": 0, "reason": "简要理由"}}, {{"entity": 2, "community": -1, "reason": "..."}}, ...]
"""

MERGE_PROMPT = """你是摘要归并助手。把一个新实体的信息合并进已有社区摘要。

已有社区摘要:
{old_summary}

新实体:
{entity_name}: {entity_description}

要求:
- 生成一段合并后的新摘要（80 字以内）
- 只保留两个来源中实际存在的信息，禁止编造
- 如果新实体没有带来新信息，保持原摘要基本不变

新摘要:"""

MERGE_BATCH_PROMPT = """你是摘要归并助手。把 {n} 个新实体的信息一次性合并进已有社区摘要。

已有社区摘要:
{old_summary}

新实体列表:
{entities_text}

要求:
- 生成一段合并后的新摘要（100 字以内）
- 只保留两个来源中实际存在的信息，禁止编造
- 如果新实体没有带来新信息，保持原摘要基本不变

新摘要:"""


class CommunityUpdater:
    def __init__(self):
        self.neo4j_client = Neo4jClient()
        self.embedding_client = EmbeddingClient()
        self.deepseek_client = DeepSeekClient()

    async def assign_entities(self, entities: List[Dict], group_id: str, progress_cb=None) -> Dict:
        """
        增量分配实体到社区（已分配的跳过，group 内闭包：候选社区只检索同 group）
        entities: 消歧后的标准实体（需已在 Neo4j，带 name_embedding）
        progress_cb: 可选协程回调 (done, total)，按阶段批量上报（前端进度展示）
        """
        self.group_id = group_id
        stats = {'assigned': 0, 'created': 0, 'skipped': 0, 'neighbor_votes': 0}
        if not entities:
            return stats

        total = len(entities)
        done = 0

        async def report(n: int):
            if progress_cb is not None:
                await progress_cb(n, total)

        start = time.time()

        # 0. 批量预过滤：已有所属社区的实体一次查出（原逐实体 _has_community，131 次往返 → 1 次）
        assigned_ids = self._ids_with_community([e['id'] for e in entities])
        pending = [e for e in entities if e['id'] not in assigned_ids]
        stats['skipped'] = len(assigned_ids)
        done += stats['skipped']
        await report(done)
        logger.info(f"[COMMUNITY_UPDATE] total={total} skip={stats['skipped']} pending={len(pending)}")

        # 1. 批量邻居投票（零 LLM，2 轮：第 2 轮吃到第 1 轮新挂的边）
        for round_no in (1, 2):
            if not pending:
                break
            winners = self._batch_neighbor_votes([e['id'] for e in pending])
            if not winners:
                break
            # 按目标社区分组，同社区的多个实体一次摘要归并
            groups: Dict[str, List[Dict]] = defaultdict(list)
            for e in pending:
                if e['id'] in winners:
                    groups[winners[e['id']][0]].append(e)
            for cid, members in groups.items():
                community = winners[members[0]['id']][1]
                await self._join_community_batch(community, members)
                stats['assigned'] += len(members)
                stats['neighbor_votes'] += len(members)
                logger.info(f"[COMMUNITY_UPDATE]   round{round_no} neighbor-vote join {cid} "
                            f"x{len(members)}（≥{NEIGHBOR_VOTE_MIN_VOTES} 票）")
            pending = [e for e in pending if e['id'] not in winners]
            done = total - len(pending)
            await report(done)

        # 2. 向量召回 + 批量 LLM 判归属
        singletons: List[Dict] = []
        if pending:
            texts = [f"{e['name']} {e.get('description', '')}" for e in pending]
            embeddings = await self.embedding_client.embed_batch(texts)

            with_candidates: List[Tuple[Dict, List[Dict]]] = []
            for e, emb in zip(pending, embeddings):
                cands = (self.neo4j_client.search_communities(emb, self.group_id, top_k=3, threshold=0.3)
                         if emb else [])
                if cands:
                    with_candidates.append((e, [c['community'] for c in cands]))
                else:
                    singletons.append(e)

            # 分批 LLM（每批 10 个实体一次调用；解析失败整批回退逐实体判断）
            join_groups: Dict[str, List[Dict]] = defaultdict(list)
            join_communities: Dict[str, Dict] = {}
            for i in range(0, len(with_candidates), LLM_JUDGE_BATCH_SIZE):
                batch = with_candidates[i:i + LLM_JUDGE_BATCH_SIZE]
                verdicts = await self._llm_judge_batch(batch)
                for (e, cands), choice in zip(batch, verdicts):
                    if choice is not None and 0 <= choice < len(cands):
                        c = cands[choice]
                        join_groups[c['id']].append(e)
                        join_communities[c['id']] = c
                    else:
                        singletons.append(e)
                done += len(batch)
                await report(done)

            for cid, members in join_groups.items():
                await self._join_community_batch(join_communities[cid], members)
                stats['assigned'] += len(members)
                logger.info(f"[COMMUNITY_UPDATE]   LLM-judge join {cid} x{len(members)}")

        # 3. 单实体社区保底（批量 embed + UNWIND 落库，不调 LLM）
        if singletons:
            await self._create_singletons_batch(singletons)
            stats['created'] += len(singletons)
            done = total
            await report(done)

        logger.info(f"[COMMUNITY_UPDATE] Done in {time.time()-start:.1f}s: {stats}")
        return stats

    # ==================== 批量查询 ====================

    def _ids_with_community(self, entity_ids: List[str]) -> set:
        """一次查询：已有 BELONGS_TO 的实体 id 集合"""
        with self.neo4j_client.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)-[:BELONGS_TO]->(:Community)
                WHERE e.id IN $ids
                RETURN e.id AS eid
            """, {'ids': entity_ids})
            return {r['eid'] for r in result}

    def _batch_neighbor_votes(self, entity_ids: List[str]) -> Dict[str, Tuple[str, Dict, int]]:
        """
        一次查询拿回所有实体的邻居社区投票明细，内存算众数。
        返回 {eid: (cid, community_props, votes)}，仅保留票数 ≥ 阈值的。
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run("""
                MATCH (c:Community {group_id: $gid})<-[:BELONGS_TO]-(m:Entity)-[:RELATES_TO]-(e:Entity)
                WHERE e.id IN $ids
                RETURN e.id AS eid, c AS community, count(*) AS votes
            """, {'gid': self.group_id, 'ids': entity_ids})
            best: Dict[str, Tuple[str, Dict, int]] = {}
            for r in result:
                eid, votes = r['eid'], r['votes']
                if votes < NEIGHBOR_VOTE_MIN_VOTES:
                    continue
                if eid not in best or votes > best[eid][2]:
                    c = dict(r['community'])
                    best[eid] = (c['id'], c, votes)
            return best

    def _entity_segment_ids_batch(self, entity_ids: List[str]) -> Dict[str, List[str]]:
        """一次查询：所有实体的溯源片段 id"""
        with self.neo4j_client.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)-[:MENTIONED_IN]->(s:Segment)
                WHERE e.id IN $ids
                RETURN e.id AS eid, s.id AS sid
            """, {'ids': entity_ids})
            mapping: Dict[str, List[str]] = defaultdict(list)
            for r in result:
                mapping[r['eid']].append(r['sid'])
            return mapping

    # ==================== 批量 LLM ====================

    async def _llm_judge_batch(self, batch: List[Tuple[Dict, List[Dict]]]) -> List[Optional[int]]:
        """
        一次 LLM 调用判断一批实体的社区归属。
        返回与 batch 等长的候选序号列表（None/-1 = 不属于任何候选）。
        整体解析失败时回退逐实体判断（保住可用性）。
        """
        blocks = []
        for i, (e, cands) in enumerate(batch):
            cand_text = "".join(
                f"  {j}. 社区（{c.get('entity_count', 0)} 个实体）: {c.get('summary', '')}\n"
                for j, c in enumerate(cands))
            blocks.append(f"实体 {i+1}: {e['name']} ({e.get('type', '')})\n"
                          f"描述: {e.get('description', '')}\n候选社区:\n{cand_text}")
        prompt = JUDGE_BATCH_PROMPT.format(n=len(batch), blocks="\n".join(blocks))

        try:
            response = await self.deepseek_client.acall_llm(prompt)
            if '```json' in response:
                response = response.split('```json')[1].split('```')[0].strip()
            elif '```' in response:
                response = response.split('```')[1].split('```')[0].strip()
            items = json.loads(response)
            if not isinstance(items, list):
                raise ValueError("not a list")
            # 防御解析：按 entity 编号取 community 序号，缺项/越界按 -1 处理
            by_entity = {}
            for it in items:
                try:
                    by_entity[int(it.get('entity'))] = int(it.get('community', -1))
                except (TypeError, ValueError):
                    continue
            verdicts = []
            for i in range(len(batch)):
                v = by_entity.get(i + 1, -1)
                verdicts.append(v if v >= 0 else None)
            return verdicts
        except Exception as e:
            logger.warning(f"[COMMUNITY_UPDATE]   batch judge failed ({e}), fallback to per-entity")
            verdicts = []
            for e, cands in batch:
                verdicts.append(await self._llm_judge_single(e, cands))
            return verdicts

    async def _llm_judge_single(self, entity: Dict, candidate_list: List[Dict]) -> Optional[int]:
        """逐实体判断（批量解析失败的回退路径）"""
        candidates_text = ""
        for idx, c in enumerate(candidate_list):
            candidates_text += f"{idx}. 社区（{c.get('entity_count', 0)} 个实体）: {c.get('summary', '')}\n\n"
        prompt = JUDGE_PROMPT.format(
            entity_name=entity['name'],
            entity_type=entity.get('type', ''),
            entity_description=entity.get('description', ''),
            candidates=candidates_text
        )
        try:
            response = await self.deepseek_client.acall_llm(prompt)
            if '```json' in response:
                response = response.split('```json')[1].split('```')[0].strip()
            elif '```' in response:
                response = response.split('```')[1].split('```')[0].strip()
            result = json.loads(response)
            idx = result.get('community_index', -1)
            return idx if 0 <= idx < len(candidate_list) else None
        except Exception as e:
            logger.warning(f"[COMMUNITY_UPDATE]   single judge failed: {e}")
            return None

    # ==================== 批量写入 ====================

    async def _join_community_batch(self, community: Dict, members: List[Dict]):
        """加入已有社区（按社区分组批量）：一次摘要归并 + UNWIND 批量挂边"""
        if not members:
            return
        cid = community['id']

        # 1. 摘要归并：同社区多个实体一次 LLM 调用（失败则保留旧摘要，不阻塞挂边）
        new_summary = community.get('summary', '')
        try:
            if len(members) == 1:
                prompt = MERGE_PROMPT.format(
                    old_summary=new_summary,
                    entity_name=members[0]['name'],
                    entity_description=members[0].get('description', ''))
            else:
                entities_text = "\n".join(
                    f"- {e['name']}: {e.get('description', '')}" for e in members)
                prompt = MERGE_BATCH_PROMPT.format(
                    n=len(members), old_summary=new_summary, entities_text=entities_text)
            merged = (await self.deepseek_client.acall_llm(prompt)).strip()
            if merged:
                new_summary = merged
        except Exception as e:
            logger.warning(f"[COMMUNITY_UPDATE]   summary merge failed for {cid}: {e}")

        summary_embedding = await self.embedding_client.embed(new_summary)

        # 2. 更新社区（entity_count 一次加 N）
        with self.neo4j_client.driver.session() as session:
            session.run("""
                MATCH (c:Community {id: $cid})
                SET c.summary = $summary,
                    c.summary_embedding = $embedding,
                    c.entity_count = c.entity_count + $n,
                    c.updated_at = datetime()
            """, {'cid': cid, 'summary': new_summary, 'embedding': summary_embedding,
                  'n': len(members)})

        # 3. 批量挂 BELONGS_TO（UNWIND 单语句，MERGE 幂等）
        with self.neo4j_client.driver.session() as session:
            session.run("""
                UNWIND $eids AS eid
                MATCH (e:Entity {id: eid})
                MATCH (c:Community {id: $cid})
                MERGE (e)-[:BELONGS_TO]->(c)
            """, {'eids': [e['id'] for e in members], 'cid': cid})

        # 4. CONTAINS 片段（溯源）：一次查出所有实体的片段，UNWIND 批量挂
        seg_map = self._entity_segment_ids_batch([e['id'] for e in members])
        pairs = [(cid, sid) for sids in seg_map.values() for sid in sids]
        if pairs:
            with self.neo4j_client.driver.session() as session:
                session.run("""
                    UNWIND $pairs AS p
                    MATCH (c:Community {id: p[0]})
                    MATCH (s:Segment {id: p[1]})
                    MERGE (c)-[:CONTAINS]->(s)
                """, {'pairs': pairs})

    async def _create_singletons_batch(self, entities: List[Dict]):
        """批量创建单实体社区（摘要直接用实体描述，不调 LLM 防幻觉；确定性 id）"""
        summaries = [f"视频提到「{e['name']}」：{e.get('description', '')}" for e in entities]
        embeddings = await self.embedding_client.embed_batch(summaries)

        rows = []
        for e, summary, emb in zip(entities, summaries, embeddings):
            cid = f"community_{hashlib.md5(e['id'].encode()).hexdigest()[:10]}"
            rows.append({
                'id': cid,
                'entity_id': e['id'],
                'group_id': self.group_id,
                'summary': summary,
                'summary_embedding': emb
            })

        # MERGE 幂等（属性对齐 save_community 的单实体社区语义）
        with self.neo4j_client.driver.session() as session:
            session.run("""
                UNWIND $rows AS r
                MERGE (c:Community {id: r.id})
                ON CREATE SET c.created_at = datetime()
                SET c.group_id = r.group_id,
                    c.level = 0,
                    c.parent_id = null,
                    c.entity_count = 1,
                    c.relationship_count = 0,
                    c.summary = r.summary,
                    c.summary_embedding = r.summary_embedding,
                    c.updated_at = datetime()
            """, {'rows': rows})

        with self.neo4j_client.driver.session() as session:
            session.run("""
                UNWIND $rows AS r
                MATCH (e:Entity {id: r.entity_id})
                MATCH (c:Community {id: r.id})
                MERGE (e)-[:BELONGS_TO]->(c)
            """, {'rows': rows})

        seg_map = self._entity_segment_ids_batch([e['id'] for e in entities])
        eid_to_cid = {r['entity_id']: r['id'] for r in rows}
        pairs = [(eid_to_cid[eid], sid) for eid, sids in seg_map.items() for sid in sids]
        if pairs:
            with self.neo4j_client.driver.session() as session:
                session.run("""
                    UNWIND $pairs AS p
                    MATCH (c:Community {id: p[0]})
                    MATCH (s:Segment {id: p[1]})
                    MERGE (c)-[:CONTAINS]->(s)
                """, {'pairs': pairs})

        logger.info(f"[COMMUNITY_UPDATE]   -> {len(entities)} singleton communities created")
