#!/usr/bin/env python3
"""
关系冲突检测
- 重复检测：同一对实体 + 相同描述
- 矛盾检测：（暂时跳过，3 个片段可能没有矛盾）
"""

import json
from typing import List, Dict, Optional
from difflib import SequenceMatcher

def calculate_similarity(s1: str, s2: str) -> float:
    """计算字符串相似度"""
    return SequenceMatcher(None, s1, s2).ratio()

class RelationshipConflictDetector:
    def __init__(self):
        self.stats = {
            'total': 0,
            'duplicates': 0,
            'conflicts': 0,
            'active': 0
        }

    def detect(self, all_segments: List[Dict], canonical_entities: List[Dict]) -> Dict:
        """
        关系冲突检测
        """
        # 构建实体名称到标准实体的映射
        entity_name_to_canonical = {}
        for canonical in canonical_entities:
            entity_name_to_canonical[canonical['name']] = canonical
            for alias in canonical.get('aliases', []):
                entity_name_to_canonical[alias] = canonical

        # 收集所有关系
        all_relationships = []
        for segment in all_segments:
            for rel in segment['relationships']:
                # 将实体名称映射到标准实体
                source_canonical = entity_name_to_canonical.get(rel['source'])
                target_canonical = entity_name_to_canonical.get(rel['target'])

                if source_canonical and target_canonical:
                    rel['source_canonical_id'] = source_canonical['id']
                    rel['target_canonical_id'] = target_canonical['id']
                    rel['source_canonical_name'] = source_canonical['name']
                    rel['target_canonical_name'] = target_canonical['name']
                    rel['segment_id'] = segment['segment_id']
                    rel['segment_index'] = segment['segment_index']
                    all_relationships.append(rel)

        self.stats['total'] = len(all_relationships)

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
            'statistics': self.stats
        }

    def _find_duplicate(self, rel: Dict, canonical_relationships: List[Dict]) -> Optional[Dict]:
        """查找重复关系"""
        for canonical in canonical_relationships:
            # 检查是否是同一对实体
            if (canonical['source_entity_id'] == rel['source_canonical_id'] and
                canonical['target_entity_id'] == rel['target_canonical_id']):

                # 检查描述是否相似
                similarity = calculate_similarity(
                    canonical['description'].lower(),
                    rel['description'].lower()
                )

                if similarity >= 0.8:  # 相似度阈值
                    return canonical

        return None

    def _create_canonical_relationship(self, rel: Dict) -> Dict:
        """创建标准关系"""
        return {
            'id': f"rel_{rel['source_canonical_id']}_{rel['target_canonical_id']}",
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

def main():
    # 加载消歧后的实体
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json', 'r', encoding='utf-8') as f:
        disambiguation_data = json.load(f)

    # 加载原始抽取结果
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r', encoding='utf-8') as f:
        all_segments = json.load(f)

    print("=== 关系冲突检测 ===")
    print(f"总关系数: {sum(len(s['relationships']) for s in all_segments)}")
    print()

    # 创建检测器
    detector = RelationshipConflictDetector()

    # 执行检测
    result = detector.detect(all_segments, disambiguation_data['canonical_entities'])

    # 保存结果
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_results.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"✅ 冲突检测完成")
    print(f"标准关系数: {result['statistics']['active']}")
    print(f"重复关系数: {result['statistics']['duplicates']}")
    print(f"\n结果已保存: {output_file}")

if __name__ == "__main__":
    main()
