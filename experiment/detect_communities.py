#!/usr/bin/env python3
"""
社区检测 - 使用 networkx 的连通分量算法
"""

import json
import requests
import networkx as nx
from typing import List, Dict, Set
from collections import defaultdict

class CommunityDetector:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def detect_communities(self, canonical_entities: List[Dict], canonical_relationships: List[Dict]) -> Dict:
        """
        社区检测主函数
        使用 networkx 的连通分量算法
        """
        # 构建图
        G = nx.Graph()

        # 添加节点
        for entity in canonical_entities:
            G.add_node(entity['id'], name=entity['name'], type=entity['type'])

        # 添加边
        for rel in canonical_relationships:
            G.add_edge(rel['source_entity_id'], rel['target_entity_id'],
                      strength=rel['strength'], description=rel['description'])

        # 使用连通分量算法
        connected_components = list(nx.connected_components(G))

        print(f"  找到 {len(connected_components)} 个连通分量")

        # 构建社区
        communities = self._build_communities(connected_components, canonical_entities, canonical_relationships)

        return {
            'communities': communities,
            'statistics': {
                'total_communities': len(communities),
                'total_entities': len(canonical_entities),
                'total_relationships': len(canonical_relationships),
                'avg_community_size': sum(c['entity_count'] for c in communities) / len(communities) if communities else 0
            }
        }

    def _build_communities(self, connected_components: List[Set[str]], entities: List[Dict], relationships: List[Dict]) -> List[Dict]:
        """
        构建社区
        """
        # 构建实体 ID 到实体的映射
        entity_map = {e['id']: e for e in entities}

        # 构建社区对象
        communities = []
        for idx, component in enumerate(connected_components):
            # 获取社区内的实体
            entity_list = [entity_map[entity_id] for entity_id in component if entity_id in entity_map]

            # 收集社区内的关系（只包含社区内部的关系）
            entity_ids = set(component)
            community_relationships = [
                rel for rel in relationships
                if rel['source_entity_id'] in entity_ids and rel['target_entity_id'] in entity_ids
            ]

            # 收集所有来源片段（追溯）
            all_sources = set()
            for entity in entity_list:
                for source in entity.get('sources', []):
                    all_sources.add(source['segment_id'])

            # 构建社区
            community = {
                'id': f"community_{idx}",
                'level': 0,
                'entity_count': len(entity_list),
                'relationship_count': len(community_relationships),
                'entities': [
                    {
                        'id': e['id'],
                        'name': e['name'],
                        'type': e['type'],
                        'description': e.get('description', ''),  # 保留描述
                        'source_count': e['source_count']
                    }
                    for e in entity_list
                ],
                'relationships': [
                    {
                        'source': rel['source_entity_name'],
                        'target': rel['target_entity_name'],
                        'description': rel['description'],
                        'strength': rel['strength']
                    }
                    for rel in community_relationships
                ],
                'source_segments': sorted(list(all_sources)),
                'summary': None
            }

            communities.append(community)

        # 按实体数量排序
        communities.sort(key=lambda x: x['entity_count'], reverse=True)

        return communities

    def generate_community_summaries(self, communities: List[Dict], segments_map: Dict = None) -> List[Dict]:
        """
        生成社区摘要（用 LLM）

        参考 Graphiti 的防幻觉机制：
        1. 孤立节点：直接用实体描述，不调 LLM
        2. 多实体社区：提供实体描述 + 关系描述 + 原文片段 + 防幻觉规则
        """
        for community in communities:
            print(f"  生成社区摘要: {community['id']} ({community['entity_count']} 个实体)")

            # 孤立节点（单实体且无关系）：直接用实体描述
            if community['entity_count'] == 1 and community['relationship_count'] == 0:
                entity = community['entities'][0]
                # 从 segments_map 获取实体的完整描述
                description = entity.get('description', '')
                community['summary'] = f"视频提到「{entity['name']}」：{description}"
                community['summary_source'] = 'entity_description'
                print(f"    → 孤立节点，使用实体描述")
                continue

            # 多实体社区：用 LLM 生成（提供完整上下文）
            prompt = self._build_summary_prompt(community, segments_map)

            # 调用 LLM
            try:
                summary = self._call_llm(prompt)
                # 添加数据溯源
                entity_names = [e['name'] for e in community['entities'][:5]]
                segment_ids = community['source_segments'][:3]
                community['summary'] = f"{summary} [来源: 实体 ({', '.join(entity_names)}), 片段 ({', '.join(segment_ids)})]"
                community['summary_source'] = 'llm_grounded'
            except Exception as e:
                print(f"    ⚠️  摘要生成失败: {e}")
                community['summary'] = f"本社区包含 {community['entity_count']} 个实体，{community['relationship_count']} 个关系。"
                community['summary_source'] = 'fallback'

        return communities

    def _build_summary_prompt(self, community: Dict, segments_map: Dict = None) -> str:
        """构建社区摘要 Prompt（带防幻觉规则）"""
        # 1. 实体名称 + 描述
        entity_info = []
        for e in community['entities']:
            desc = e.get('description', '无描述')
            entity_info.append(f"- {e['name']} ({e['type']}): {desc}")

        # 2. 关系描述
        relationship_info = []
        for r in community['relationships'][:10]:
            relationship_info.append(f"- {r['source']} → {r['target']}: {r['description']}")

        # 3. 原文片段（如果有 segments_map）
        segment_texts = []
        if segments_map:
            for seg_id in community['source_segments'][:3]:  # 最多取 3 个片段
                segment = segments_map.get(seg_id)
                if segment:
                    transcript = segment.get('transcript', '')[:300]  # 最多 300 字
                    segment_texts.append(f"[片段 {seg_id}]\n{transcript}...")

        return f"""请根据以下信息生成社区摘要（50-100字）。

**严格规则（必须遵守）**：
1. 只使用下面提供的信息，不要加入任何原文没有的内容
2. 不要推断、不要扩展、不要泛化
3. 直接陈述事实，不要用"成员们探讨"、"社区讨论"等措辞
4. 不要提到"社区"、"成员"等词，直接说视频讲了什么
5. 如果信息不足，直接列出实体和关系即可

**社区包含的实体及其描述**:
{chr(10).join(entity_info)}

**社区包含的关系**:
{chr(10).join(relationship_info) if relationship_info else "（无关系）"}

**相关视频片段原文**:
{chr(10).join(segment_texts) if segment_texts else "（无原文）"}

请生成一个简短的摘要，说明视频中这些实体和关系讨论了什么内容。

摘要:"""

    def _call_llm(self, prompt: str) -> str:
        """调用 LLM"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "max_tokens": 200
        }

        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()

        result = response.json()
        return result['choices'][0]['message']['content'].strip()

def main():
    # 加载消歧后的实体
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json', 'r', encoding='utf-8') as f:
        disambiguation_data = json.load(f)

    # 加载冲突检测后的关系
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_results.json', 'r', encoding='utf-8') as f:
        conflict_data = json.load(f)

    # 加载原始抽取结果（用于获取原文片段）
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r', encoding='utf-8') as f:
        extraction_data = json.load(f)

    # 构建 segments_map（segment_id -> segment）
    segments_map = {seg['segment_id']: seg for seg in extraction_data}

    canonical_entities = disambiguation_data['canonical_entities']
    canonical_relationships = conflict_data['canonical_relationships']

    print("=== 社区检测（使用连通分量算法）===")
    print(f"总实体数: {len(canonical_entities)}")
    print(f"总关系数: {len(canonical_relationships)}")
    print()

    # 加载配置
    env = {}
    with open('/mnt/Data/projs/Java/DOVideo-AI/.env', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key] = value

    # 创建检测器
    detector = CommunityDetector(
        api_key=env.get('SILICONFLOW_API_KEY'),
        base_url=env.get('SILICONFLOW_BASE_URL', 'https://api.siliconflow.cn/v1'),
        model=env.get('LLM_MODEL', 'deepseek-ai/DeepSeek-V3.2')
    )

    # 执行社区检测
    result = detector.detect_communities(canonical_entities, canonical_relationships)

    print(f"✅ 社区检测完成")
    print(f"社区数: {result['statistics']['total_communities']}")
    print(f"平均社区大小: {result['statistics']['avg_community_size']:.1f}")
    print()

    # 生成社区摘要（传入 segments_map）
    print("=== 生成社区摘要 ===")
    communities = detector.generate_community_summaries(result['communities'], segments_map)
    result['communities'] = communities

    # 保存结果
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/community_detection_results.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 结果已保存: {output_file}")

    # 打印社区列表
    print("\n=== 社区列表 ===")
    for community in result['communities']:
        print(f"\n社区 {community['id']}:")
        print(f"  实体数: {community['entity_count']}")
        print(f"  关系数: {community['relationship_count']}")
        print(f"  来源片段数: {len(community['source_segments'])}")
        print(f"  摘要来源: {community.get('summary_source', 'unknown')}")
        print(f"  摘要: {community['summary']}")
        print(f"  关键实体: {', '.join([e['name'] for e in community['entities'][:5]])}")

if __name__ == "__main__":
    main()
