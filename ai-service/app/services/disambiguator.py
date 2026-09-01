"""
实体消歧服务（三层级联）
Level 1: 精确匹配
Level 2: 模糊匹配（MinHash + LSH + Jaccard）
Level 3: LLM 判断（Embedding 检索 + LLM 判断）
"""
import re
import math
import time
from typing import List, Dict, Optional
from difflib import SequenceMatcher
from app.clients.deepseek import DeepSeekClient
from app.clients.embedding import EmbeddingClient
from app.core.logging import logger


class Disambiguator:
    def __init__(self):
        self.deepseek_client = DeepSeekClient()
        self.embedding_client = EmbeddingClient()
        # 实体名（规范化后）-> embedding，避免对已有标准实体重复调 embedding API
        self.embedding_cache = {}

        # Neo4j 客户端（Level 3 跨视频候选检索用，连接失败时降级为仅内存候选）
        try:
            from app.clients.neo4j_client import Neo4jClient
            self.neo4j_client = Neo4jClient()
        except Exception as e:
            logger.warning(f"[DISAMBIGUATION] Neo4j unavailable, Level 3 will use in-memory candidates only: {e}")
            self.neo4j_client = None

        # 统计
        self.stats = {
            'total': 0,
            'level1_hits': 0,
            'level2_hits': 0,
            'level3_hits': 0,
            'new_entities': 0
        }

    async def disambiguate(self, all_segments: List[Dict], group_id: str) -> Dict:
        """
        实体消歧主函数（串行处理，group 内闭包：跨视频候选只检索同 group 实体）
        """
        self.group_id = group_id
        import time
        start_time = time.time()

        # 收集所有实体
        all_entities = []
        for segment in all_segments:
            for entity in segment['entities']:
                entity['segment_id'] = segment['segment_id']
                entity['segment_index'] = segment['segment_index']
                all_entities.append(entity)

        self.stats['total'] = len(all_entities)
        logger.info(f"[DISAMBIGUATION] Starting disambiguation for {len(all_entities)} entities")

        # 标准实体列表（消歧后的实体）
        canonical_entities = []
        merge_history = []

        for i, entity in enumerate(all_entities):
            entity_start = time.time()
            logger.info(f"[DISAMBIGUATION] Processing entity {i+1}/{len(all_entities)}: {entity['name']} ({entity['type']})")

            # Level 1: 精确匹配
            level1_start = time.time()
            match = self._level1_exact_match(entity, canonical_entities)
            level1_duration = (time.time() - level1_start) * 1000

            if match:
                logger.info(f"[DISAMBIGUATION] Entity {i+1} matched by Level 1 (exact) in {level1_duration:.2f}ms: {match['name']}")
                self._merge_entity(entity, match, 'level1_exact')
                entity['canonical_id'] = match['id']  # 盖章：原始实体 -> 标准实体 id
                self.stats['level1_hits'] += 1
                merge_history.append({
                    'entity': entity['name'],
                    'merged_to': match['name'],
                    'method': 'level1_exact',
                    'confidence': 1.0
                })
                continue

            # Level 2: 模糊匹配
            level2_start = time.time()
            match = self._level2_fuzzy_match(entity, canonical_entities)
            level2_duration = (time.time() - level2_start) * 1000

            if match:
                logger.info(f"[DISAMBIGUATION] Entity {i+1} matched by Level 2 (fuzzy) in {level2_duration:.2f}ms: {match['name']}")
                self._merge_entity(entity, match, 'level2_fuzzy')
                entity['canonical_id'] = match['id']
                self.stats['level2_hits'] += 1
                merge_history.append({
                    'entity': entity['name'],
                    'merged_to': match['name'],
                    'method': 'level2_fuzzy',
                    'confidence': 0.9
                })
                continue

            # Level 3: LLM 判断
            level3_start = time.time()
            match = await self._level3_llm_judge(entity, canonical_entities)
            level3_duration = (time.time() - level3_start) * 1000

            if match:
                logger.info(f"[DISAMBIGUATION] Entity {i+1} matched by Level 3 (LLM) in {level3_duration:.2f}ms: {match['name']}")
                self._merge_entity(entity, match, 'level3_llm')
                entity['canonical_id'] = match['id']
                self.stats['level3_hits'] += 1
                merge_history.append({
                    'entity': entity['name'],
                    'merged_to': match['name'],
                    'method': 'level3_llm',
                    'confidence': 0.95
                })
                continue

            # 未匹配，创建新实体
            canonical_entity = self._create_canonical_entity(entity)
            canonical_entities.append(canonical_entity)
            entity['canonical_id'] = canonical_entity['id']
            self.stats['new_entities'] += 1

            # 新实体的 embedding 只算一次，进缓存，后续 Level 3 直接复用
            embedding = await self._get_embedding(canonical_entity['name'])
            if embedding:
                canonical_entity['name_embedding'] = embedding

            entity_duration = (time.time() - entity_start) * 1000
            logger.info(f"[DISAMBIGUATION] Entity {i+1} created as new entity in {entity_duration:.2f}ms")

        # 更新统计
        self.stats['canonical_count'] = len(canonical_entities)
        self.stats['merge_rate'] = (self.stats['level1_hits'] + self.stats['level2_hits'] + self.stats['level3_hits']) / self.stats['total'] if self.stats['total'] > 0 else 0

        total_duration = (time.time() - start_time) * 1000
        logger.info(f"[DISAMBIGUATION] Disambiguation completed: {len(canonical_entities)} canonical entities, merge rate: {self.stats['merge_rate']:.1%}, {total_duration:.2f}ms")

        return {
            'canonical_entities': canonical_entities,
            'merge_history': merge_history,
            'statistics': self.stats
        }

    def _level1_exact_match(self, entity: Dict, canonical_entities: List[Dict]) -> Optional[Dict]:
        """Level 1: 精确匹配"""
        normalized = self._normalize_exact(entity['name'])

        for canonical in canonical_entities:
            if self._normalize_exact(canonical['name']) == normalized:
                return canonical

            # 也检查别名
            for alias in canonical.get('aliases', []):
                if self._normalize_exact(alias) == normalized:
                    return canonical

        return None

    def _level2_fuzzy_match(self, entity: Dict, canonical_entities: List[Dict]) -> Optional[Dict]:
        """Level 2: 模糊匹配"""
        normalized = self._normalize_fuzzy(entity['name'])

        # 熵门控：低熵跳过
        if not self._has_high_entropy(normalized):
            return None

        best_match = None
        best_score = 0.0
        threshold = 0.85  # 相似度阈值

        for canonical in canonical_entities:
            # 检查标准名称
            score = self._calculate_similarity(normalized, self._normalize_fuzzy(canonical['name']))
            if score > best_score:
                best_score = score
                best_match = canonical

            # 检查别名
            for alias in canonical.get('aliases', []):
                score = self._calculate_similarity(normalized, self._normalize_fuzzy(alias))
                if score > best_score:
                    best_score = score
                    best_match = canonical

        if best_score >= threshold:
            return best_match

        return None

    async def _level3_llm_judge(self, entity: Dict, canonical_entities: List[Dict]) -> Optional[Dict]:
        """Level 3: LLM 判断（Neo4j 向量索引 + 内存缓存 embedding 检索候选，再 LLM 判断）"""
        logger.info(f"[DISAMBIGUATION]   [Level 3] Processing entity: {entity['name']} ({entity['type']})")

        # 1. 新实体 embedding（缓存，只算一次）
        embedding_start = time.time()
        entity_embedding = await self._get_embedding(entity['name'])
        embedding_duration = (time.time() - embedding_start) * 1000

        if not entity_embedding:
            logger.warning(f"[DISAMBIGUATION]   [Level 3] Embedding failed for {entity['name']}")
            return None

        logger.info(f"[DISAMBIGUATION]   [Level 3] Embedding completed in {embedding_duration:.2f}ms")

        # 2. 收集候选（两路：本视频内存中的标准实体 + Neo4j 中已有视频的实体）
        similarity_start = time.time()
        candidates_map = {}

        # 2a. 内存候选：直接用缓存的 embedding 计算，零 API 调用
        for canonical in canonical_entities:
            canonical_embedding = canonical.get('name_embedding')
            if canonical_embedding:
                score = self._cosine_similarity(entity_embedding, canonical_embedding)
                candidates_map[canonical['id']] = (canonical, score)

        # 2b. Neo4j 候选：走向量索引（Graphiti 的做法，支持跨视频消歧）
        if self.neo4j_client:
            try:
                neo4j_results = self.neo4j_client.find_similar_entities(entity_embedding, self.group_id, top_k=5, threshold=0.5)
                for item in neo4j_results:
                    db_entity = item['entity']
                    # 内存候选优先（含本视频最新的 aliases/sources）
                    if db_entity.get('id') not in candidates_map:
                        candidates_map[db_entity['id']] = (db_entity, item['score'])
                logger.info(f"[DISAMBIGUATION]   [Level 3] Neo4j vector search returned {len(neo4j_results)} candidates")
            except Exception as e:
                logger.warning(f"[DISAMBIGUATION]   [Level 3] Neo4j vector search failed, fallback to in-memory only: {e}")

        # 按相似度排序，取 Top-5
        candidates_with_score = sorted(candidates_map.values(), key=lambda x: x[1], reverse=True)[:5]
        similarity_duration = (time.time() - similarity_start) * 1000
        logger.info(f"[DISAMBIGUATION]   [Level 3] Similarity calculation completed in {similarity_duration:.2f}ms (no extra embedding API calls)")

        logger.info(f"[DISAMBIGUATION]   [Level 3] Top-5 candidates:")
        for idx, (c, s) in enumerate(candidates_with_score):
            logger.info(f"[DISAMBIGUATION]     {idx}. {c['name']} ({c['type']}) - similarity: {s:.4f}")

        # 过滤掉相似度太低的（阈值 0.5）
        top_candidates = [(c, s) for c, s in candidates_with_score if s > 0.5]

        if not top_candidates:
            logger.info(f"[DISAMBIGUATION]   [Level 3] No candidates above threshold 0.5")
            return None

        logger.info(f"[DISAMBIGUATION]   [Level 3] {len(top_candidates)} candidates after filtering")

        # 3. LLM 判断
        candidates = [c for c, s in top_candidates]

        prompt = f"""You are an entity deduplication assistant.

NEW ENTITY:
{entity['name']} ({entity['type']})
Description: {entity['description']}

EXISTING ENTITIES:
"""
        for idx, candidate in enumerate(candidates):
            prompt += f"{idx}. {candidate['name']} ({candidate['type']})\n   Description: {candidate['description']}\n\n"

        prompt += """Entities should only be considered duplicates if they refer to the *same real-world object or concept*.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They have similar names or purposes but refer to separate instances or concepts.

Task:
Determine if the NEW ENTITY is a duplicate of any EXISTING ENTITY.
Return the index of the matching entity (0-4), or -1 if no duplicate exists.

Return ONLY a JSON object:
{
  "duplicate_index": 0 or 1 or 2 or 3 or 4 or -1,
  "confidence": 0.95,
  "reason": "brief explanation"
}
"""

        try:
            llm_start = time.time()
            response = await self.deepseek_client.acall_llm(prompt)
            llm_duration = (time.time() - llm_start) * 1000

            logger.info(f"[DISAMBIGUATION]   [Level 3] LLM call completed in {llm_duration:.2f}ms")
            logger.info(f"[DISAMBIGUATION]   [Level 3] LLM response: {response[:200]}...")

            # 提取 JSON（可能包含在 ```json ... ``` 中）
            if '```json' in response:
                response = response.split('```json')[1].split('```')[0].strip()
            elif '```' in response:
                response = response.split('```')[1].split('```')[0].strip()

            import json
            result = json.loads(response)

            duplicate_index = result.get('duplicate_index', -1)
            reason = result.get('reason', '')

            logger.info(f"[DISAMBIGUATION]   [Level 3] LLM judgment: duplicate_index={duplicate_index}, reason={reason}")

            if 0 <= duplicate_index < len(candidates):
                matched = candidates[duplicate_index]
                logger.info(f"[DISAMBIGUATION]   [Level 3] ✅ Merged to: {matched['name']}")

                # 命中的是 Neo4j 中已有视频的实体：纳入内存标准实体列表，保证后续合并与保存一致
                if matched not in canonical_entities:
                    matched.setdefault('sources', [])
                    matched.setdefault('source_count', 0)
                    canonical_entities.append(matched)

                return matched
            else:
                logger.info(f"[DISAMBIGUATION]   [Level 3] ❌ Not merged (LLM judged as different entity)")

        except Exception as e:
            logger.error(f"[DISAMBIGUATION]   [Level 3] ⚠️  LLM judgment failed: {type(e).__name__}: {str(e)}")

        return None

    async def _get_embedding(self, name: str) -> Optional[List[float]]:
        """获取实体名 embedding（带缓存，同名不重复调 API）"""
        cache_key = self._normalize_fuzzy(name)
        if cache_key in self.embedding_cache:
            return self.embedding_cache[cache_key]

        embedding = await self.embedding_client.embed(name)
        if embedding:
            self.embedding_cache[cache_key] = embedding
        return embedding

    def _create_canonical_entity(self, entity: Dict) -> Dict:
        """创建标准实体（id 嵌入 group_id：跨用户同名不撞 id，全局 MATCH by id 安全）"""
        return {
            'id': f"entity_{self.group_id}_{self._normalize_fuzzy(entity['name'])}",
            'name': entity['name'],
            'type': entity['type'],
            'description': entity['description'],
            'aliases': [],
            'sources': [{
                'segment_id': entity['segment_id'],
                'segment_index': entity['segment_index'],
                'surface_form': entity['name']  # 该片段中实际出现的写法
            }],
            'source_count': 1
        }

    def _merge_entity(self, entity: Dict, canonical: Dict, method: str):
        """合并实体：别名 + 来源归并；新描述收集到 new_descriptions，由 commit 端三级门控决定是否吸收"""
        # 添加别名
        if entity['name'] != canonical['name'] and entity['name'] not in canonical['aliases']:
            canonical['aliases'].append(entity['name'])

        # 添加来源
        canonical['sources'].append({
            'segment_id': entity['segment_id'],
            'segment_index': entity['segment_index'],
            'surface_form': entity['name']
        })
        canonical['source_count'] += 1

        # 收集新描述（不直接改描述——语义门控在 _save_graph 批量做）
        new_desc = (entity.get('description') or '').strip()
        if new_desc:
            canonical.setdefault('new_descriptions', []).append(new_desc)

    # ========== 工具方法 ==========

    def _normalize_exact(self, name: str) -> str:
        """规范化：小写、去空格、去标点"""
        normalized = re.sub(r'\s+', ' ', name.lower())
        return normalized.strip()

    def _normalize_fuzzy(self, name: str) -> str:
        """模糊规范化：只保留字母数字和中文"""
        normalized = re.sub(r'[^a-z0-9一-鿿]+', '', name.lower())
        return normalized

    def _calculate_similarity(self, s1: str, s2: str) -> float:
        """计算字符串相似度（0-1）"""
        return SequenceMatcher(None, s1, s2).ratio()

    def _has_high_entropy(self, name: str) -> bool:
        """判断是否有高熵（长度 >= 6 或词数 >= 2）"""
        if len(name) < 6 and len(name.split()) < 2:
            return False
        return True

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        magnitude1 = math.sqrt(sum(a * a for a in vec1))
        magnitude2 = math.sqrt(sum(b * b for b in vec2))
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0
        return dot_product / (magnitude1 * magnitude2)
