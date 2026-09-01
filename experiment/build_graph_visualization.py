#!/usr/bin/env python3
"""
构建知识图谱可视化（带社区）
"""

import json

def main():
    # 加载消歧后的实体
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json', 'r', encoding='utf-8') as f:
        disambiguation_data = json.load(f)

    # 加载冲突检测后的关系
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_results.json', 'r', encoding='utf-8') as f:
        conflict_data = json.load(f)

    # 加载社区检测结果
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/community_detection_results.json', 'r', encoding='utf-8') as f:
        community_data = json.load(f)

    canonical_entities = disambiguation_data['canonical_entities']
    canonical_relationships = conflict_data['canonical_relationships']
    communities = community_data['communities']

    # 构建实体到社区的映射
    entity_to_community = {}
    for community in communities:
        for entity in community['entities']:
            entity_to_community[entity['id']] = community['id']

    # 构建节点和边
    nodes = []
    edges = []

    # 社区颜色映射
    community_colors = [
        '#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA07A', '#98D8C8',
        '#F7B731', '#A4B0BE', '#6C5CE7', '#FD79A8', '#FDCB6E'
    ]

    for entity in canonical_entities:
        community_id = entity_to_community.get(entity['id'], 'unknown')

        # 获取社区颜色
        if community_id != 'unknown':
            # 从 communities 列表中找到对应的社区索引
            community_index = next((idx for idx, c in enumerate(communities) if c['id'] == community_id), 0)
            color = community_colors[community_index % len(community_colors)]
        else:
            color = '#A4B0BE'  # 默认灰色

        nodes.append({
            'id': entity['id'],
            'label': entity['name'],
            'title': f"类型: {entity['type']}<br>描述: {entity['description']}<br>来源: {entity['source_count']} 个片段<br>社区: {community_id}",
            'color': {
                'background': color,
                'border': color,
                'highlight': {
                    'background': color,
                    'border': '#000000'
                }
            },
            'size': 20 + entity['source_count'] * 5
        })

    for rel in canonical_relationships:
        edges.append({
            'from': rel['source_entity_id'],
            'to': rel['target_entity_id'],
            'label': rel['description'][:20],
            'title': f"描述: {rel['description']}<br>强度: {rel['strength']}<br>来源: {rel['source_count']} 个片段",
            'arrows': 'to',
            'width': rel['strength'] / 2
        })

    # 生成 HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>知识图谱可视化 - 中国人牛肉自由</title>
    <script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
        }}
        h1 {{
            color: #333;
        }}
        .stats {{
            background: white;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .stats p {{
            margin: 5px 0;
        }}
        #graph {{
            width: 100%;
            height: 700px;
            background: white;
            border: 1px solid #ddd;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .legend {{
            background: white;
            padding: 15px;
            border-radius: 5px;
            margin-top: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .legend-item {{
            display: inline-block;
            margin-right: 20px;
            margin-bottom: 10px;
        }}
        .legend-color {{
            display: inline-block;
            width: 20px;
            height: 20px;
            border-radius: 3px;
            vertical-align: middle;
            margin-right: 5px;
        }}
        .community-section {{
            background: white;
            padding: 15px;
            border-radius: 5px;
            margin-top: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .community-card {{
            border-left: 4px solid #4CAF50;
            padding: 10px;
            margin: 10px 0;
            background: #f9f9f9;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            margin-top: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 12px;
            text-align: left;
        }}
        th {{
            background: #4CAF50;
            color: white;
        }}
        tr:nth-child(even) {{
            background: #f9f9f9;
        }}
    </style>
</head>
<body>
    <h1>知识图谱可视化 - 中国人牛肉自由</h1>

    <div class="stats">
        <h2>统计概览</h2>
        <p><strong>总实体数:</strong> {len(canonical_entities)}</p>
        <p><strong>总关系数:</strong> {len(canonical_relationships)}</p>
        <p><strong>社区数:</strong> {len(communities)}</p>
        <p><strong>合并的实体数:</strong> {disambiguation_data['statistics']['total'] - disambiguation_data['statistics']['canonical_count']}</p>
        <p><strong>重复的关系数:</strong> {conflict_data['statistics']['duplicates']}</p>
    </div>

    <h2>知识图谱（按社区着色）</h2>
    <div id="graph"></div>

    <div class="legend">
        <h3>社区图例</h3>
"""

    for idx, community in enumerate(communities):
        color = community_colors[idx % len(community_colors)]
        html += f"""
        <div class="legend-item"><span class="legend-color" style="background: {color};"></span>{community['id']} ({community['entity_count']} 个实体)</div>
"""

    html += """
    </div>

    <div class="community-section">
        <h2>社区列表</h2>
"""

    for idx, community in enumerate(communities, 1):
        color = community_colors[(idx-1) % len(community_colors)]
        html += f"""
        <div class="community-card" style="border-left-color: {color};">
            <h3>社区 {idx}: {community['id']}</h3>
            <p><strong>实体数:</strong> {community['entity_count']} | <strong>关系数:</strong> {community['relationship_count']} | <strong>来源片段数:</strong> {len(community['source_segments'])}</p>
            <p><strong>摘要:</strong> {community['summary']}</p>
            <p><strong>关键实体:</strong> {', '.join([e['name'] for e in community['entities'][:5]])}</p>
            <p><strong>来源片段:</strong> {', '.join(community['source_segments'])}</p>
        </div>
"""

    html += """
    </div>

    <h2>实体列表</h2>
    <table>
        <tr>
            <th>序号</th>
            <th>实体名称</th>
            <th>类型</th>
            <th>社区</th>
            <th>描述</th>
            <th>来源片段数</th>
        </tr>
"""

    for idx, entity in enumerate(canonical_entities, 1):
        community_id = entity_to_community.get(entity['id'], 'unknown')
        html += f"""
        <tr>
            <td>{idx}</td>
            <td>{entity['name']}</td>
            <td>{entity['type']}</td>
            <td>{community_id}</td>
            <td>{entity['description'][:50]}...</td>
            <td>{entity['source_count']}</td>
        </tr>
"""

    html += """
    </table>

    <h2>关系列表</h2>
    <table>
        <tr>
            <th>序号</th>
            <th>源实体</th>
            <th>目标实体</th>
            <th>描述</th>
            <th>强度</th>
            <th>来源片段数</th>
        </tr>
"""

    for idx, rel in enumerate(canonical_relationships, 1):
        html += f"""
        <tr>
            <td>{idx}</td>
            <td>{rel['source_entity_name']}</td>
            <td>{rel['target_entity_name']}</td>
            <td>{rel['description'][:50]}...</td>
            <td>{rel['strength']}</td>
            <td>{rel['source_count']}</td>
        </tr>
"""

    html += f"""
    </table>

    <script>
        const nodes = new vis.DataSet({json.dumps(nodes, ensure_ascii=False)});
        const edges = new vis.DataSet({json.dumps(edges, ensure_ascii=False)});

        const container = document.getElementById('graph');
        const data = {{ nodes, edges }};
        const options = {{
            nodes: {{
                shape: 'dot',
                font: {{ size: 14 }},
                borderWidth: 2
            }},
            edges: {{
                font: {{ size: 12, align: 'middle' }},
                smooth: {{ type: 'continuous' }}
            }},
            physics: {{
                stabilization: true,
                barnesHut: {{
                    gravitationalConstant: -3000,
                    springLength: 200,
                    springConstant: 0.04
                }}
            }},
            interaction: {{
                hover: true,
                tooltipDelay: 100
            }}
        }};

        new vis.Network(container, data, options);
    </script>
</body>
</html>
"""
    # 保存 HTML
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/knowledge_graph.html'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"✅ 知识图谱可视化已生成: {output_file}")
    print(f"总实体数: {len(canonical_entities)}")
    print(f"总关系数: {len(canonical_relationships)}")
    print(f"社区数: {len(communities)}")

if __name__ == "__main__":
    main()
