#!/usr/bin/env python3
"""
详细分析 Label Propagation 的迭代过程
"""

import json
from typing import List, Dict
from collections import defaultdict

def label_propagation_detailed(graph: Dict[str, Dict[str, int]], entity_map: Dict[str, str], max_iterations: int = 100) -> Dict[str, int]:
    """
    Label Propagation（打印详细过程）
    """
    # 初始化：每个节点是独立的社区
    community_map = {node: i for i, node in enumerate(graph.keys())}

    print("=== 初始状态 ===")
    for node, community in sorted(community_map.items(), key=lambda x: x[1]):
        print(f"  {entity_map.get(node, node)}: 社区 {community}")
    print()

    for iteration in range(max_iterations):
        print(f"=== 第 {iteration + 1} 次迭代 ===")
        no_change = True
        new_community_map = {}

        for node, neighbors in graph.items():
            node_name = entity_map.get(node, node)

            if not neighbors:
                new_community_map[node] = community_map[node]
                print(f"  {node_name}: 孤立节点，保持社区 {community_map[node]}")
                continue

            # 统计邻居的社区分布
            community_scores = defaultdict(int)
            for neighbor, edge_count in neighbors.items():
                neighbor_community = community_map[neighbor]
                community_scores[neighbor_community] += edge_count

            # 选择得分最高的社区
            if community_scores:
                best_community = max(community_scores.items(), key=lambda x: x[1])[0]
                new_community_map[node] = best_community

                neighbor_names = [entity_map.get(n, n) for n in neighbors.keys()]
                neighbor_communities = [community_map[n] for n in neighbors.keys()]

                print(f"  {node_name}:")
                print(f"    邻居: {', '.join(neighbor_names)}")
                print(f"    邻居社区: {neighbor_communities}")
                print(f"    社区得分: {dict(community_scores)}")
                print(f"    选择: 社区 {best_community} (原社区: {community_map[node]})")

                if best_community != community_map[node]:
                    no_change = False
                    print(f"    → 改变！")
            else:
                new_community_map[node] = community_map[node]

        community_map = new_community_map
        print()

        if no_change:
            print(f"  收敛于第 {iteration + 1} 次迭代")
            break

    return community_map

def main():
    # 加载数据
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json', 'r', encoding='utf-8') as f:
        disambiguation_data = json.load(f)

    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_results.json', 'r', encoding='utf-8') as f:
        conflict_data = json.load(f)

    canonical_entities = disambiguation_data['canonical_entities']
    canonical_relationships = conflict_data['canonical_relationships']

    # 构建图（邻接表）
    graph = defaultdict(lambda: defaultdict(int))

    for rel in canonical_relationships:
        source_id = rel['source_entity_id']
        target_id = rel['target_entity_id']

        graph[source_id][target_id] += 1
        graph[target_id][source_id] += 1

    # 构建实体映射
    entity_map = {e['id']: e['name'] for e in canonical_entities}

    # 运行详细的 Label Propagation
    community_map = label_propagation_detailed(graph, entity_map, max_iterations=5)

    # 按社区分组
    community_entities = defaultdict(list)
    for entity in canonical_entities:
        community_id = community_map.get(entity['id'], -1)
        community_entities[community_id].append(entity['name'])

    # 打印最终社区
    print("\n=== 最终社区划分 ===")
    for community_id, entity_names in sorted(community_entities.items()):
        print(f"社区 {community_id}: {', '.join(entity_names)}")

if __name__ == "__main__":
    main()
