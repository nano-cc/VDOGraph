#!/usr/bin/env python3
"""
复现 Label Propagation 的问题
展示：A、B、C 都连接到 D，但 A、B、C 被划分到一个社区，D 被划分到另一个社区
"""

import json
from typing import List, Dict
from collections import defaultdict

def label_propagation_buggy(graph: Dict[str, Dict[str, int]], max_iterations: int = 100) -> Dict[str, int]:
    """
    我之前的 Label Propagation 实现（有问题）
    """
    # 初始化：每个节点是独立的社区
    community_map = {node: i for i, node in enumerate(graph.keys())}

    for iteration in range(max_iterations):
        no_change = True
        new_community_map = {}

        for node, neighbors in graph.items():
            if not neighbors:
                # 孤立节点，保持原社区
                new_community_map[node] = community_map[node]
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

                if best_community != community_map[node]:
                    no_change = False
            else:
                new_community_map[node] = community_map[node]

        community_map = new_community_map

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

        # 无向图（双向）
        graph[source_id][target_id] += 1
        graph[target_id][source_id] += 1

    print("=== 图结构 ===")
    print(f"节点数: {len(graph)}")
    print(f"边数: {sum(len(neighbors) for neighbors in graph.values()) // 2}")
    print()

    # 打印每个节点的邻居
    print("=== 每个节点的邻居 ===")
    entity_map = {e['id']: e['name'] for e in canonical_entities}
    for node, neighbors in graph.items():
        node_name = entity_map.get(node, node)
        neighbor_names = [entity_map.get(n, n) for n in neighbors.keys()]
        print(f"{node_name}: {', '.join(neighbor_names)}")
    print()

    # 运行有问题的 Label Propagation
    print("=== 运行 Label Propagation（有问题的实现）===")
    community_map = label_propagation_buggy(graph)
    print()

    # 按社区分组
    community_entities = defaultdict(list)
    for entity in canonical_entities:
        community_id = community_map.get(entity['id'], -1)
        community_entities[community_id].append(entity['name'])

    # 打印社区
    print("=== 社区划分结果 ===")
    for community_id, entity_names in sorted(community_entities.items()):
        print(f"社区 {community_id}:")
        print(f"  实体数: {len(entity_names)}")
        print(f"  实体: {', '.join(entity_names)}")

        # 检查社区内部的连通性
        entity_ids = [e['id'] for e in canonical_entities if e['name'] in entity_names]
        internal_edges = 0
        for rel in canonical_relationships:
            if rel['source_entity_id'] in entity_ids and rel['target_entity_id'] in entity_ids:
                internal_edges += 1

        print(f"  内部边数: {internal_edges}")

        if internal_edges == 0 and len(entity_names) > 1:
            print(f"  ⚠️  问题：多个实体但没有内部边！")
        print()

if __name__ == "__main__":
    main()
