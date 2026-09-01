"""
关系冲突检测服务
关系对齐：优先用消歧阶段盖在原始实体上的 canonical_id（Graphiti 式），
降级链：片段内规范化匹配 -> 全局别名表（规范化）-> 丢弃并记日志
"""
from typing import List, Dict, Optional
import re
from difflib import SequenceMatcher
from app.core.logging import logger


def _normalize(name: str) -> str:
    """与消歧 L1 同款规范化：小写、去多余空格"""
    return re.sub(r'\s+', ' ', (name or '').lower()).strip()


class ConflictDetector:
    def __init__(self):
        self.stats = {
            'total': 0,
            'duplicates': 0,
            'conflicts': 0,
            'active': 0,
            'dropped': 0
        }

    async def detect(self, all_segments: List[Dict], canonical_entities: List[Dict]) -> Dict:
        """
        关系冲突检测
        """
        # 全局降级映射：规范化（标准名/别名）-> 标准实体
        global_map = {}
        id_to_canonical = {}
        for canonical in canonical_entities:
            global_map[_normalize(canonical['name'])] = canonical
            id_to_canonical[canonical['id']] = canonical
            for alias in canonical.get('aliases', []):
                global_map[_normalize(alias)] = canonical

        dropped = []

        # 收集所有关系
        all_relationships = []
        for segment in all_segments:
            # 片段内映射：规范化实体名 -> canonical_id（消歧时盖的章）
            local_map = {}
            for entity in segment.get('entities', []):
                if entity.get('canonical_id'):
                    local_map[_normalize(entity['name'])] = entity['canonical_id']

            for rel in segment['relationships']:
                # 优先：片段内盖章的 canonical_id；降级：全局别名表（规范化）
                source_id = local_map.get(_normalize(rel['source']))
                target_id = local_map.get(_normalize(rel['target']))
                if not source_id:
                    c = global_map.get(_normalize(rel['source']))
                    source_id = c['id'] if c else None
                if not target_id:
                    c = global_map.get(_normalize(rel['target']))
                    target_id = c['id'] if c else None

                if source_id and target_id:
                    rel['source_canonical_id'] = source_id
                    rel['target_canonical_id'] = target_id
                    rel['source_canonical_name'] = id_to_canonical[source_id]['name']
                    rel['target_canonical_name'] = id_to_canonical[target_id]['name']
                    rel['segment_id'] = segment['segment_id']
                    rel['segment_index'] = segment['segment_index']
                    all_relationships.append(rel)
                else:
                    dropped.append({
                        'segment_id': segment['segment_id'],
                        'source': rel['source'],
                        'target': rel['target'],
                        'reason': f"{'source' if not source_id else 'target'} 实体名未映射: "
                                  f"{rel['source'] if not source_id else rel['target']}"
                    })

        self.stats['total'] = len(all_relationships)
        self.stats['dropped'] = len(dropped)
        if dropped:
            logger.warning(f"[CONFLICT] {len(dropped)} relationships dropped (unmapped entity names):")
            for d in dropped:
                logger.warning(f"[CONFLICT]   dropped: {d['source']} -> {d['target']} ({d['reason']})")

        # 检测重复
        canonical_relationships = []
        duplicate_groups = []

        for rel in all_relationships:
            # 查找重复
            duplicate = self._find_duplicate(rel, canonical_relationships)

            if duplicate:
                # 合并到现有关系
                duplicate['source_count'] += 1
                duplicate['sources'].append({
                    'segment_id': rel['segment_id'],
                    'segment_index': rel['segment_index']
                })
                self.stats['duplicates'] += 1

                # 记录重复组
                found_group = False
                for group in duplicate_groups:
                    if group['canonical_id'] == duplicate['id']:
                        group['duplicates'].append(rel)
                        found_group = True
                        break

                if not found_group:
                    duplicate_groups.append({
                        'canonical_id': duplicate['id'],
                        'canonical_rel': duplicate,
                        'duplicates': [rel]
                    })
            else:
                # 创建新的标准关系
                canonical_rel = self._create_canonical_relationship(rel)
                canonical_relationships.append(canonical_rel)

        self.stats['active'] = len(canonical_relationships)

        return {
            'canonical_relationships': canonical_relationships,
            'duplicate_groups': duplicate_groups,
            'dropped': dropped,
            'statistics': self.stats
        }

    def _find_duplicate(self, rel: Dict, canonical_relationships: List[Dict]) -> Optional[Dict]:
        """查找重复关系"""
        for canonical in canonical_relationships:
            # 检查是否是同一对实体
            if (canonical['source_entity_id'] == rel['source_canonical_id'] and
                canonical['target_entity_id'] == rel['target_canonical_id']):

                # 检查描述是否相似
                similarity = self._calculate_similarity(
                    canonical['description'].lower(),
                    rel['description'].lower()
                )

                if similarity >= 0.8:  # 相似度阈值
                    return canonical

        return None

    def _create_canonical_relationship(self, rel: Dict) -> Dict:
        """创建标准关系"""
        import hashlib
        # id 包含描述哈希：同一实体对的不同描述是不同关系，且重跑时幂等
        desc_hash = hashlib.md5(rel['description'].strip().lower().encode()).hexdigest()[:8]
        return {
            'id': f"rel_{rel['source_canonical_id']}_{rel['target_canonical_id']}_{desc_hash}",
            'source_entity_id': rel['source_canonical_id'],
            'target_entity_id': rel['target_canonical_id'],
            'source_entity_name': rel['source_canonical_name'],
            'target_entity_name': rel['target_canonical_name'],
            'description': rel['description'],
            'strength': int(rel['strength']),
            'source_count': 1,
            'sources': [{
                'segment_id': rel['segment_id'],
                'segment_index': rel['segment_index']
            }],
            'status': 'active'
        }

    def _calculate_similarity(self, s1: str, s2: str) -> float:
        """计算字符串相似度"""
        return SequenceMatcher(None, s1, s2).ratio()
