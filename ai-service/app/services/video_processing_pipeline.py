"""
完整视频处理 Pipeline（两阶段）
- analyze: 解析 + 抽取（无锁并发，视频私有数据，可直接调用）
- commit: 消歧 + 去重 + 入库 + 社区增量（图写入，调用方需持 Redisson 全局写锁 kg:graph:write）
- process: analyze + commit 组合（单机测试用）
"""
import time
import json
import asyncio
from typing import Dict, List
from app.services.video_parser import VideoParser
from app.services.entity_extractor import EntityExtractor
from app.services.disambiguator import Disambiguator
from app.services.conflict_detector import ConflictDetector
from app.services.community_updater import CommunityUpdater
from app.clients.neo4j_client import Neo4jClient
from app.clients.embedding import EmbeddingClient
from app.clients.deepseek import DeepSeekClient
from app.core.logging import logger


class VideoProcessingPipeline:
    def __init__(self):
        self.video_parser = VideoParser()
        self.entity_extractor = EntityExtractor()
        self.disambiguator = Disambiguator()
        self.conflict_detector = ConflictDetector()
        self.community_updater = CommunityUpdater()
        self.neo4j_client = Neo4jClient()
        self.embedding_client = EmbeddingClient()
        self.deepseek_client = DeepSeekClient()

    # ==================== 阶段一：analyze（无锁并发） ====================

    async def analyze(self, video_path: str, media_id: int, user_goal: str = "", force: bool = False,
                      progress_cb=None, group_id: str = None) -> Dict:
        """
        解析 + 抽取（幂等：已解析/已抽取的部分直接跳过）
        progress_cb: 可选协程回调 (done, total)，逐片段抽取完成时上报进度
        group_id: 用户隔离标识（三期），Media/Segment 节点落 group_id 属性
        """
        self.group_id = group_id
        start_time = time.time()
        logger.info(f"[ANALYZE] Starting: {video_path} (media_id={media_id}, force={force})")

        # HTTP 视频源（MinIO 等）：先下载到本地临时文件
        actual_path = video_path
        if video_path.startswith('http://') or video_path.startswith('https://'):
            import requests
            import tempfile
            import os
            download_start = time.time()
            suffix = os.path.splitext(video_path.split('?')[0])[1] or '.mp4'
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            with requests.get(video_path, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    tmp.write(chunk)
            tmp.close()
            actual_path = tmp.name
            logger.info(f"[ANALYZE] Downloaded video to {actual_path} ({(time.time()-download_start)*1000:.0f}ms)")

        try:
            # 1. 视频解析（幂等：Neo4j 已有该视频的片段则跳过）
            cached_segments = [] if force else self.neo4j_client.get_media_segments(media_id)
            raw_extraction_cache = {}

            if cached_segments:
                from app.models.video import VideoContext, VideoSegment
                video_context = VideoContext(
                    source=video_path,
                    user_goal=user_goal,
                    segments=[
                        VideoSegment(
                            start_ms=s['start_ms'],
                            end_ms=s['end_ms'],
                            transcript=s['transcript'],
                            ocr_texts=s.get('ocr_texts', []),
                            evidence_frames=s.get('frame_urls', [])
                        )
                        for s in cached_segments
                    ]
                )
                for s in cached_segments:
                    if s.get('raw_extraction'):
                        raw_extraction_cache[s['id']] = json.loads(s['raw_extraction'])
                logger.info(f"[ANALYZE] Media {media_id} already parsed, loaded {len(cached_segments)} segments "
                            f"({len(raw_extraction_cache)} with extraction cache)")
            else:
                video_context = await self.video_parser.parse(actual_path, user_goal, media_id=media_id)
                video_context.source = video_path
                # 解析完立即保存 Media + Segment（断点续跑基础）
                await self._save_media_and_segments(video_context, media_id)

            # 2. 实体关系抽取（并发，逐片段幂等跳过 + 落库）
            from app.models.entity import SegmentExtraction
            to_extract = []
            for i, segment in enumerate(video_context.segments):
                segment_id = f"media_{media_id}_segment_{i}"  # 顺序号（静音感知分片后 start_ms 不再对齐 60s 网格）
                if segment_id in raw_extraction_cache:
                    continue
                to_extract.append((i, segment, segment_id))

            logger.info(f"[ANALYZE] Extraction: {len(to_extract)} segments to extract, "
                        f"{len(raw_extraction_cache)} cached")

            total_segments = len(video_context.segments)
            done_count = len(raw_extraction_cache)
            if progress_cb is not None:
                await progress_cb(done_count, total_segments)

            if to_extract:
                seg_inputs = [
                    SegmentExtraction(
                        segment_id=segment_id,
                        segment_index=i,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        transcript=segment.transcript,
                        ocr_texts=segment.ocr_texts,
                        entities=[],
                        relationships=[]
                    )
                    for i, segment, segment_id in to_extract
                ]

                async def on_segment_done(i, segment):
                    nonlocal done_count
                    done_count += 1
                    if progress_cb is not None:
                        await progress_cb(done_count, total_segments)

                extract_result = await self.entity_extractor.extract(seg_inputs, on_segment_done=on_segment_done)
                # 每片段抽完立即落库（断点续跑）；失败的片段不缓存，下次重试
                failed = 0
                for (i, segment, segment_id), result in zip(to_extract, extract_result):
                    if result.extraction_failed:
                        failed += 1
                        continue
                    self.neo4j_client.save_segment_extraction(
                        segment_id,
                        json.dumps(result.model_dump(), ensure_ascii=False)
                    )
                if failed:
                    logger.warning(f"[ANALYZE] {failed} segments failed extraction (not cached, will retry on next run)")

            total_duration = (time.time() - start_time) * 1000
            stats = {
                'segments': len(video_context.segments),
                'extracted_now': len(to_extract),
                'extraction_cached': len(raw_extraction_cache)
            }
            logger.info(f"[ANALYZE] Completed: {stats}, {total_duration:.2f}ms")
            return {'status': 'success', 'media_id': media_id, 'statistics': stats, 'duration_ms': total_duration}

        except Exception as e:
            logger.error(f"[ANALYZE] Failed: {e}", exc_info=True)
            raise

    # ==================== 阶段二：commit（需持全局写锁） ====================

    async def commit(self, media_id: int, progress_cb=None, group_id: str = None) -> Dict:
        """
        图写入：消歧 → 关系去重 → 入库 → 社区增量（全部在 group 内闭包）
        数据全部从 Neo4j 读取（analyze 阶段的产出）
        progress_cb: 可选协程回调 (stage)，各阶段开始时上报
        """
        self.group_id = group_id
        start_time = time.time()
        logger.info(f"[COMMIT] Starting: media_id={media_id}")

        async def report(stage: str):
            if progress_cb is not None:
                await progress_cb(stage)

        try:
            # 1. 从 Neo4j 加载抽取结果
            await report("加载抽取结果")
            segments = self.neo4j_client.get_media_segments(media_id)
            extract_dicts = []
            for s in segments:
                if s.get('raw_extraction'):
                    extract_dicts.append(json.loads(s['raw_extraction']))
            if not extract_dicts:
                raise ValueError(f"media {media_id} 没有已抽取的片段，请先运行 analyze")
            logger.info(f"[COMMIT] Loaded {len(extract_dicts)} extracted segments from Neo4j")

            # 2. 实体消歧（串行，全局锁内）
            await report("实体消歧")
            disambiguation_start = time.time()
            disambiguation_result = await self.disambiguator.disambiguate(extract_dicts, self.group_id)
            logger.info(f"[COMMIT] Disambiguation: {disambiguation_result['statistics']['canonical_count']} canonical entities, "
                        f"{(time.time()-disambiguation_start)*1000:.2f}ms")

            # 3. 关系对齐去重（串行）
            await report("关系对齐去重")
            conflict_result = await self.conflict_detector.detect(extract_dicts, disambiguation_result['canonical_entities'])
            logger.info(f"[COMMIT] Conflict detection: {conflict_result['statistics']['active']} relationships, "
                        f"dropped={conflict_result['statistics']['dropped']}")

            # 4. 入库（批量 embedding + 批量 MERGE）
            await report("图谱入库")
            save_start = time.time()
            await self._save_graph(disambiguation_result, conflict_result)
            logger.info(f"[COMMIT] Graph saved in {(time.time()-save_start)*1000:.2f}ms")

            # 5. 社区增量更新（串行，锁内；进度回调透传 x/y 给前端）
            await report("社区增量更新")
            community_start = time.time()

            async def community_progress(done: int, total: int):
                await report(f"社区增量更新 {done}/{total}")

            community_stats = await self.community_updater.assign_entities(
                disambiguation_result['canonical_entities'], self.group_id,
                progress_cb=community_progress)
            logger.info(f"[COMMIT] Community assignment: {community_stats}, {(time.time()-community_start)*1000:.2f}ms")

            total_duration = (time.time() - start_time) * 1000
            return {
                'status': 'success',
                'media_id': media_id,
                'statistics': {
                    'entities': disambiguation_result['statistics']['canonical_count'],
                    'relationships': conflict_result['statistics']['active'],
                    'relationships_dropped': conflict_result['statistics']['dropped'],
                    'communities': community_stats
                },
                'duration_ms': total_duration
            }

        except Exception as e:
            logger.error(f"[COMMIT] Failed: {e}", exc_info=True)
            raise

    # ==================== 两跳 commit（#68，docs/kg-phase4-commit-optimization.md） ====================

    async def prepare_commit_two_hop(self, media_id: int, group_id: str, progress_cb=None) -> Dict:
        """
        两跳 commit 的 prepare（锁外，多视频并行）：
        读 raw_extraction → 第一跳视频内预合并 → 对图 L1/L2 → 批量 LLM 判重 → 关系锚定
        产出 CommitPlan（决议表 + 写集 + 快照时间），一行图都不写
        """
        import hashlib
        from app.services.intra_video_merger import merge_video_extractions, normalize_exact, normalize_fuzzy
        from app.services.batch_disambiguator import BatchDisambiguator

        self.group_id = group_id
        start = time.time()
        logger.info(f"[COMMIT-2HOP] prepare starting: media_id={media_id}")

        if progress_cb:
            await progress_cb("加载抽取结果")
        segments = self.neo4j_client.get_media_segments(media_id)
        extract_dicts = [json.loads(s['raw_extraction']) for s in segments if s.get('raw_extraction')]
        if not extract_dicts:
            raise ValueError(f"media {media_id} 没有已抽取的片段，请先运行 analyze")

        raw_entities, raw_relations = [], []
        for d in extract_dicts:
            for e in d.get('entities', []):
                raw_entities.append({'name': e['name'], 'type': e.get('type', 'Other'),
                                     'description': e.get('description', ''),
                                     'segment_index': d.get('segment_index', 0)})
            for r in d.get('relationships', []):
                raw_relations.append({'source': r.get('source', ''), 'target': r.get('target', ''),
                                      'description': r.get('description', ''),
                                      'strength': r.get('strength', 5),
                                      'segment_index': d.get('segment_index', 0)})

        if progress_cb:
            await progress_cb("视频内预合并")
        entity_groups, video_relations, borderline = merge_video_extractions(raw_entities, raw_relations)

        if progress_cb:
            await progress_cb("图级判重")
        context_text = (extract_dicts[0].get('transcript') or '')
        bd = BatchDisambiguator(group_id)
        resolution_plan = await bd.resolve(entity_groups, borderline, context_text)

        # 关系锚定：端点组 → 决议 → 最终实体 id
        res_by_name: Dict[str, object] = {}
        for res in resolution_plan.resolutions:
            for sf in res.group.surface_forms:
                res_by_name[normalize_exact(sf)] = res

        final_relations, dropped = [], 0
        for vr in video_relations:
            rs = res_by_name.get(normalize_exact(vr.source))
            rt = res_by_name.get(normalize_exact(vr.target))
            if not rs or not rt:
                dropped += 1
                continue
            src_id, tgt_id = rs.target_entity_id, rt.target_entity_id
            if src_id == tgt_id:
                continue  # 自环（同组内关系）丢弃
            desc = max(vr.descriptions, key=len) if vr.descriptions else ''
            rel_id = f"rel_{src_id}_{tgt_id}_{hashlib.md5(normalize_fuzzy(desc).encode()).hexdigest()[:10]}"
            final_relations.append({
                'id': rel_id, 'source_entity_id': src_id, 'target_entity_id': tgt_id,
                'description': desc, 'strength': vr.strength,
                'source_count': vr.source_count,
                'source_segment_ids': [f"media_{media_id}_segment_{idx}" for idx in vr.sources],
            })

        plan = {
            'media_id': media_id,
            'group_id': group_id,
            'resolutions': resolution_plan.resolutions,
            'final_relations': final_relations,
            'relations_dropped': dropped,
            'write_set': sorted({r.target_entity_id for r in resolution_plan.resolutions}),
            'snapshot_time': time.time(),
            'stats': resolution_plan.stats,
        }
        logger.info(f"[COMMIT-2HOP] prepare done: media_id={media_id} "
                    f"entities={len(resolution_plan.resolutions)} relations={len(final_relations)} "
                    f"dropped={dropped} write_set={len(plan['write_set'])} "
                    f"duration={time.time()-start:.1f}s")
        return plan

    async def apply_commit_two_hop(self, plan: Dict, progress_cb=None) -> Dict:
        """
        两跳 commit 的 apply（细粒度多锁，秒级）：
        幻影检查 + 增量复查 → MERGE 实体/关系 → 社区应用（per-group 社区锁）
        """
        from app.core.kglock import KgLock, KgMultiLock

        media_id = plan['media_id']
        group_id = plan['group_id']
        self.group_id = group_id
        start = time.time()
        logger.info(f"[COMMIT-2HOP] apply starting: media_id={media_id} write_set={len(plan['write_set'])}")

        lock_keys = [f"kg:lock:entity:{eid}" for eid in plan['write_set']]
        async with KgMultiLock(lock_keys, wait_timeout_s=300):
            if progress_cb:
                await progress_cb("幻影检查")
            entity_payloads = await self._build_entity_payloads(plan)

            if progress_cb:
                await progress_cb("图谱入库")
            await self._save_graph_two_hop(media_id, entity_payloads, plan['final_relations'])

        # 社区：per-group 社区锁（比图锁窄，只串行社区更新）
        if progress_cb:
            await progress_cb("社区增量更新")

        async def community_progress(done: int, total: int):
            if progress_cb:
                await progress_cb(f"社区增量更新 {done}/{total}")

        community_candidates = [
            {'id': p['id'], 'name': p['name'], 'type': p['type'], 'description': p['description']}
            for p in entity_payloads
        ]
        async with KgLock(f"kg:lock:community:{group_id}"):
            community_stats = await self.community_updater.assign_entities(
                community_candidates, group_id, progress_cb=community_progress)

        duration = time.time() - start
        logger.info(f"[COMMIT-2HOP] apply done: media_id={media_id} duration={duration:.1f}s "
                    f"communities={community_stats}")
        return {
            'status': 'success',
            'media_id': media_id,
            'statistics': {
                'entities': len(entity_payloads),
                'relationships': len(plan['final_relations']),
                'relationships_dropped': plan['relations_dropped'],
                'communities': community_stats,
                'prepare_stats': plan['stats'],
            },
            'duration_ms': duration * 1000,
        }

    async def commit_two_hop(self, media_id: int, progress_cb=None, group_id: str = None) -> Dict:
        """两跳 commit 组合入口（prepare 锁外 + apply 细粒度锁）"""
        plan = await self.prepare_commit_two_hop(media_id, group_id, progress_cb)
        return await self.apply_commit_two_hop(plan, progress_cb)

    async def _build_entity_payloads(self, plan: Dict) -> List[Dict]:
        """
        apply 锁内：构造实体写入载荷。
        - merge：读图里最新状态（别名并集 + 描述进门控），幻影检查顺带完成
          （create 的确定性 id 此刻已存在 → 说明别人抢先建了，降级为 merge）
        - 增量复查：快照后新建的实体与我的新建实体向量比对，语义撞车降级 merge
        """
        import hashlib
        group_id = plan['group_id']
        graph_index = await asyncio.to_thread(self.neo4j_client.get_group_entities, group_id)
        graph_by_id = {e['id']: e for e in graph_index}

        # 增量复查：快照之后新建的实体（语义幻影检测）
        delta_entities = await asyncio.to_thread(
            self.neo4j_client.get_entities_created_after, group_id, plan['snapshot_time'])

        payloads = []
        for res in plan['resolutions']:
            g = res.group
            target_id = res.target_entity_id
            action = res.action

            # 幻影检查 ①：create 的确定性 id 已被别人建了 → 降级 merge
            existing = graph_by_id.get(target_id)
            if action == 'create' and existing is not None:
                action = 'merge'
                logger.info(f"[COMMIT-2HOP] 幻影降级：{g.name} 的 id {target_id} 已存在，转 merge")

            # 幻影检查 ②：语义幻影（别人新建了近似名实体）
            if action == 'create' and delta_entities:
                my_emb = await self.embedding_client.embed(res.chosen_name)
                if my_emb:
                    for de in delta_entities:
                        if de['id'] == target_id:
                            continue
                        de_emb = de.get('name_embedding')
                        if not de_emb:
                            continue
                        sim = self._cosine(my_emb, de_emb)
                        if sim >= 0.9:
                            action = 'merge'
                            target_id = de['id']
                            existing = graph_by_id.get(target_id)
                            logger.info(f"[COMMIT-2HOP] 语义幻影降级：{res.chosen_name} → {de['name']}（sim={sim:.3f}）")
                            break

            if action == 'merge' and existing is not None:
                aliases = sorted((set(existing.get('aliases') or []) | set(g.surface_forms)
                                  | {existing['name']}) - {res.chosen_name})
                payloads.append({
                    'id': target_id, 'group_id': group_id, 'name': res.chosen_name,
                    'type': g.type,
                    'description': existing.get('description') or '',
                    'new_descriptions': list(g.descriptions),
                    'aliases': aliases,
                    'source_count': (existing.get('source_count') or 0) + g.mention_count,
                    'name_embedding': None,  # 名字可能变（chosen_name），后面统一补
                    '_group': g,
                })
            else:
                # create
                base_desc = '\n'.join(g.descriptions)
                payloads.append({
                    'id': target_id, 'group_id': group_id, 'name': res.chosen_name,
                    'type': g.type, 'description': base_desc,
                    'aliases': sorted(set(g.surface_forms) - {res.chosen_name}),
                    'source_count': g.mention_count,
                    'name_embedding': None,
                    '_group': g,
                })
        return payloads

    async def _save_graph_two_hop(self, media_id: int, payloads: List[Dict], final_relations: List[Dict]):
        """两跳 apply 的写图：复用三级门控 + 批量 MERGE；关系先读旧值并集 source_segment_ids"""
        # 实体 embedding：name 全部补（chosen_name 可能与已有 name_embedding 不一致）
        name_embs = await self.embedding_client.embed_batch([p['name'] for p in payloads])
        for p, emb in zip(payloads, name_embs):
            p['name_embedding'] = emb

        desc_embs = await self.embedding_client.embed_batch([p['description'] or ' ' for p in payloads])
        await self._merge_entity_descriptions(payloads, desc_embs)

        self.neo4j_client.save_entities_batch([
            {
                'id': p['id'], 'group_id': p['group_id'], 'name': p['name'], 'type': p['type'],
                'description': p['description'],
                'name_embedding': p.get('name_embedding'),
                'description_embedding': desc_embs[i],
                'aliases': p['aliases'],
                'source_count': p['source_count'],
            }
            for i, p in enumerate(payloads)
        ])

        # MENTIONED_IN 溯源边
        links = []
        for p in payloads:
            g = p.pop('_group', None)
            if not g:
                continue
            for idx in g.sources:
                links.append({
                    'entity_id': p['id'],
                    'segment_id': f"media_{media_id}_segment_{idx}",
                    'segment_index': idx,
                    'confidence': 0.9,
                    'source_type': 'asr',
                    'surface_form': g.name,
                })
        if links:
            self.neo4j_client.link_entity_to_segment_batch(links)

        # 关系：读旧值并集后 MERGE（跨视频/重跑不丢来源）
        if final_relations:
            existing = await asyncio.to_thread(
                self._get_relationships_by_ids, [r['id'] for r in final_relations])
            rel_embs = await self.embedding_client.embed_batch([r['description'] for r in final_relations])
            batch = []
            for i, r in enumerate(final_relations):
                old = existing.get(r['id'])
                if old:
                    seg_ids = sorted(set(old.get('source_segment_ids') or []) | set(r['source_segment_ids']))
                    r['source_segment_ids'] = seg_ids
                    r['source_count'] = len(seg_ids)
                batch.append({
                    'id': r['id'], 'group_id': self.group_id,
                    'source_entity_id': r['source_entity_id'],
                    'target_entity_id': r['target_entity_id'],
                    'description': r['description'],
                    'strength': r['strength'], 'confidence': 0.9,
                    'description_embedding': rel_embs[i],
                    'source_count': r.get('source_count', 1),
                    'source_segment_ids': r['source_segment_ids'],
                })
            self.neo4j_client.save_relationships_batch(batch)

    def _get_relationships_by_ids(self, rel_ids: List[str]) -> Dict[str, Dict]:
        """批量读已有关系（合并 source_segment_ids 用）"""
        if not rel_ids:
            return {}
        with self.neo4j_client.driver.session() as session:
            result = session.run("""
                UNWIND $ids AS rid
                MATCH ()-[r:RELATES_TO {id: rid}]->()
                RETURN r.id AS id, r.source_segment_ids AS source_segment_ids
            """, {'ids': rel_ids})
            return {row['id']: {'source_segment_ids': row['source_segment_ids']} for row in result}

    # ==================== 组合（单机测试用） ====================

    async def process(self, video_path: str, media_id: int, user_goal: str = "", force: bool = False) -> Dict:
        """analyze + commit 组合（注意：无锁，仅单机测试用；生产经 Java 编排持锁调 commit）"""
        analyze_result = await self.analyze(video_path, media_id, user_goal, force)
        commit_result = await self.commit(media_id)
        return {
            'status': 'success',
            'media_id': media_id,
            'statistics': {
                'segments': analyze_result['statistics']['segments'],
                'entities': commit_result['statistics']['entities'],
                'relationships': commit_result['statistics']['relationships'],
                'communities': commit_result['statistics']['communities']
            },
            'duration_ms': analyze_result['duration_ms'] + commit_result['duration_ms']
        }

    # ==================== 内部方法 ====================

    async def _save_media_and_segments(self, video_context, media_id):
        """保存视频节点和片段（解析完成后立即调用，批量 embedding）"""
        import os

        media_node_id = f"media_{media_id}"
        title = os.path.basename(video_context.source)
        duration_ms = max((s.end_ms for s in video_context.segments), default=0)
        logger.info(f"[NEO4J] Saving media: {media_node_id} ({title})")
        title_embedding = await self.embedding_client.embed(title)
        self.neo4j_client.save_media({
            'id': media_node_id,
            'group_id': self.group_id,
            'title': title,
            'path': video_context.source,
            'duration_ms': duration_ms,
            'title_embedding': title_embedding
        })

        # 批量 embedding：所有片段的 transcript + ocr 一次批量
        logger.info(f"[NEO4J] Saving {len(video_context.segments)} segments")
        texts = []
        for segment in video_context.segments:
            texts.append(segment.transcript or " ")
            texts.append(" ".join(segment.ocr_texts) or " ")
        embeddings = await self.embedding_client.embed_batch(texts)

        for idx, segment in enumerate(video_context.segments):
            segment_id = f"media_{media_id}_segment_{idx}"  # 顺序号（静音感知分片后 start_ms 不再对齐 60s 网格）
            self.neo4j_client.save_segment({
                'id': segment_id,
                'group_id': self.group_id,
                'media_id': media_id,
                'segment_index': idx,
                'start_ms': segment.start_ms,
                'end_ms': segment.end_ms,
                'transcript': segment.transcript,
                'ocr_texts': segment.ocr_texts,
                'frame_urls': segment.evidence_frames,
                'transcript_embedding': embeddings[idx * 2],
                'ocr_embedding': embeddings[idx * 2 + 1]
            })
            self.neo4j_client.link_segment_to_media(segment_id, media_node_id)

    async def _save_graph(self, disambiguation_result, conflict_result):
        """保存实体/关系（批量 embedding + 批量 MERGE）"""
        entities = disambiguation_result['canonical_entities']

        # 批量 embedding：消歧阶段已算的 name_embedding 复用，缺的批量补
        missing_name = [e for e in entities if not e.get('name_embedding')]
        if missing_name:
            name_embs = await self.embedding_client.embed_batch([e['name'] for e in missing_name])
            for e, emb in zip(missing_name, name_embs):
                e['name_embedding'] = emb

        desc_texts = [e['description'] for e in entities]
        desc_embs = await self.embedding_client.embed_batch(desc_texts)

        # 描述归并三级门控（graphiti 追加 + GraphRAG 压缩的折中）：
        # ① 字面包含 → 跳过  ② 语义重复（余弦 ≥ 阈值）→ 跳过  ③ 有新信息 → 追加，超长批量 LLM 压缩
        await self._merge_entity_descriptions(entities, desc_embs)

        logger.info(f"[NEO4J] Batch saving {len(entities)} entities")
        self.neo4j_client.save_entities_batch([
            {
                'id': e['id'],
                'group_id': self.group_id,
                'name': e['name'],
                'type': e['type'],
                'description': e['description'],
                'name_embedding': e.get('name_embedding'),
                'description_embedding': desc_embs[i],
                'aliases': e.get('aliases', []),
                'source_count': e.get('source_count', 1)
            }
            for i, e in enumerate(entities)
        ])

        # 批量链接实体到片段（溯源，带 surface_form）
        links = []
        for e in entities:
            for source in e.get('sources', []):
                links.append({
                    'entity_id': e['id'],
                    'segment_id': source['segment_id'],
                    'segment_index': source['segment_index'],
                    'confidence': 0.9,
                    'source_type': 'asr',
                    'surface_form': source.get('surface_form')
                })
        logger.info(f"[NEO4J] Batch linking {len(links)} entity-segment edges")
        self.neo4j_client.link_entity_to_segment_batch(links)

        # 关系：批量 embedding + 批量 MERGE
        rels = conflict_result['canonical_relationships']
        if rels:
            rel_embs = await self.embedding_client.embed_batch([r['description'] for r in rels])
            logger.info(f"[NEO4J] Batch saving {len(rels)} relationships")
            self.neo4j_client.save_relationships_batch([
                {
                    'id': r['id'],
                    'group_id': self.group_id,
                    'source_entity_id': r['source_entity_id'],
                    'target_entity_id': r['target_entity_id'],
                    'description': r['description'],
                    'strength': r['strength'],
                    'confidence': 0.9,
                    'description_embedding': rel_embs[i],
                    'source_count': r.get('source_count', 1),
                    'source_segment_ids': [s['segment_id'] for s in r.get('sources', []) if s.get('segment_id')]
                }
                for i, r in enumerate(rels)
            ])

    # ==================== 描述归并（三级门控） ====================

    DESC_SIMILARITY_THRESHOLD = 0.88  # 语义重复阈值（高于则跳过）
    MAX_DESC_CHARS = 800              # 追加后超此长度 → LLM 压缩
    COMPRESSED_DESC_CHARS = 400       # 压缩目标长度

    async def _merge_entity_descriptions(self, entities, desc_embs):
        """
        判同命中的实体描述归并：
        - 新描述的 embedding 捎带批量算（每个实体最终描述的 embedding 本来就要算，净增调用≈0）
        - 语义重复不吸收（信息冗余）；有新信息追加；追加后超长才 LLM 压缩（每实体每 commit 最多 1 次）
        """
        # 收集所有新描述，一次批量 embedding
        new_desc_index = []  # [(entity_idx, text)]
        for i, e in enumerate(entities):
            for nd in e.pop('new_descriptions', []):
                nd = nd.strip()
                if nd:
                    new_desc_index.append((i, nd))
        if not new_desc_index:
            return

        new_embs = await self.embedding_client.embed_batch([t for _, t in new_desc_index])
        logger.info(f"[DESC_MERGE] {len(new_desc_index)} new descriptions from merged entities")

        changed = 0
        for (i, nd), nd_emb in zip(new_desc_index, new_embs):
            e = entities[i]
            cur = (e.get('description') or '').strip()
            # ① 字面包含：新描述已被覆盖 → 跳过
            if nd in cur:
                continue
            # ② 语义重复：与当前描述（其 embedding 在上面批量里已算）余弦超阈值 → 跳过
            if nd_emb and i < len(desc_embs) and desc_embs[i]:
                sim = self._cosine(nd_emb, desc_embs[i])
                if sim >= self.DESC_SIMILARITY_THRESHOLD:
                    continue
            # ③ 有新信息 → 追加
            e['description'] = (cur + '\n' + nd).strip() if cur else nd
            e['_desc_changed'] = True
            changed += 1

        # 超长描述批量压缩（每实体最多 1 次 LLM）
        to_compress = [e for e in entities if len(e.get('description', '')) > self.MAX_DESC_CHARS]
        for e in to_compress:
            compressed = await self._compress_description(e['name'], e['description'])
            if compressed:
                e['description'] = compressed
                e['_desc_changed'] = True

        # 变更过的描述重算 embedding（入库 embedding 必须与最终文本一致）
        changed_idx = [i for i, e in enumerate(entities) if e.pop('_desc_changed', False)]
        if changed_idx:
            embs = await self.embedding_client.embed_batch([entities[i]['description'] for i in changed_idx])
            for i, emb in zip(changed_idx, embs):
                desc_embs[i] = emb

        logger.info(f"[DESC_MERGE] appended={changed} compressed={len(to_compress)} reembedded={len(changed_idx)}")

    async def _compress_description(self, name: str, description: str):
        """LLM 压缩超长实体描述（GraphRAG 式归并，仅在超阈值时触发）"""
        prompt = f"""你是知识图谱实体描述的压缩器。将以下关于实体「{name}」的冗余描述压缩为不超过 {self.COMPRESSED_DESC_CHARS} 字的简洁描述：
- 保留全部关键事实（时间、地点、人物、事件、数字）
- 去除重复和啰嗦的表达
- 只输出压缩后的描述文本本身，不要任何额外内容

描述：
{description}"""
        try:
            result = await self.deepseek_client.acall_llm(prompt)
            result = result.strip()
            if result and len(result) < len(description):
                return result
        except Exception as e:
            logger.warning(f"[DESC_MERGE] compress failed for {name}: {e}")
        return None

    @staticmethod
    def _cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0
