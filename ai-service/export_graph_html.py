#!/usr/bin/env python3
"""
从 Neo4j 导出知识图谱可视化 HTML
- 实体按 L0 社区着色，显示层次结构
- 点击实体查看描述、别名、来源片段（时间戳 + 证据帧）
- 点击关系查看描述和来源
"""
import sys
import json
sys.path.insert(0, '.')
from app.clients.neo4j_client import Neo4jClient

OUTPUT = '/mnt/Data/projs/Java/DOVideo-AI/experiment/knowledge_graph_neo4j.html'

# 社区配色（L0）
COLORS = [
    '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
    '#42d4f4', '#f032e6', '#bfef45', '#469990', '#9A6324',
    '#800000', '#aaffc3', '#808000', '#000075', '#a9a9a9',
]

client = Neo4jClient()
with client.driver.session() as session:
    # 实体 + 所属社区（L0）+ 来源片段
    entities = {}
    for r in session.run("""
        MATCH (e:Entity)
        OPTIONAL MATCH (e)-[:BELONGS_TO]->(c:Community)
        OPTIONAL MATCH (e)-[:MENTIONED_IN]->(s:Segment)
        RETURN e, collect(DISTINCT {id: c.id, level: c.level, summary: c.summary}) as communities,
               collect(DISTINCT {id: s.id, start_ms: s.start_ms, end_ms: s.end_ms, media_id: s.media_id, frame_urls: s.frame_urls}) as segments
    """):
        e = dict(r['e'])
        e.pop('name_embedding', None)
        e.pop('description_embedding', None)
        l0 = [c for c in r['communities'] if c['id'] and c['level'] == 0]
        e['_l0_community'] = l0[0]['id'] if l0 else None
        e['_segments'] = [s for s in r['segments'] if s['id']]
        # 供图级 Leiden 溯源用
        e['sources'] = [{'segment_id': s['id']} for s in e['_segments']]
        entities[e['id']] = e

    relationships = []
    for r in session.run("""
        MATCH (s:Entity)-[rel:RELATES_TO]->(t:Entity)
        RETURN s.id as src, t.id as tgt, rel.description as d, rel.strength as w, rel.source_segment_ids as segs
    """):
        relationships.append({'src': r['src'], 'tgt': r['tgt'], 'd': r['d'], 'w': r['w'], 'segs': r['segs'] or []})

    communities = []
    for r in session.run("MATCH (c:Community) RETURN c ORDER BY c.level, c.entity_count DESC"):
        c = dict(r['c'])
        c.pop('summary_embedding', None)
        communities.append(c)

    media_count = session.run("MATCH (m:Media) RETURN count(m) as c").single()['c']
    segment_count = session.run("MATCH (s:Segment) RETURN count(s) as c").single()['c']

client.close()

# 图级 Leiden 划分（跨视频社区，用于着色和面板展示；不调 LLM，只算结构）
from app.services.community_detector import CommunityDetector

rel_for_leiden = [
    {'source_entity_id': r['src'], 'target_entity_id': r['tgt'],
     'source_entity_name': '', 'target_entity_name': '',
     'description': r['d'], 'strength': r['w'] or 1}
    for r in relationships
]
detector = CommunityDetector()
graph_communities = detector._hierarchical_leiden(list(entities.values()), rel_for_leiden)

# 实体 -> 图级 L0 社区
entity_to_l0 = {}
for c in graph_communities:
    if c.level == 0:
        for e in c.entities:
            entity_to_l0[e.id] = c.id

l0_ids = sorted(set(entity_to_l0.values()))
color_map = {cid: COLORS[i % len(COLORS)] for i, cid in enumerate(l0_ids)}

nodes = []
for e in entities.values():
    color = color_map.get(entity_to_l0.get(e['id']), '#cccccc')
    seg_info = ''.join(
        f"<li>媒体 {s['media_id']} 片段 {s['id'].split('_segment_')[-1]}: "
        f"{s['start_ms']//1000}s - {s['end_ms']//1000}s</li>"
        for s in e['_segments']
    )
    nodes.append({
        'id': e['id'],
        'label': e['name'],
        'color': color,
        'title': (
            f"<b>{e['name']}</b> ({e['type']})<br>"
            f"{e.get('description', '')[:200]}<br>"
            f"别名: {', '.join(e.get('aliases', [])) or '无'}<br>"
            f"<b>来源片段:</b><ul>{seg_info}</ul>"
        ),
        'value': e.get('source_count', 1),
    })

edges = [
    {
        'from': r['src'], 'to': r['tgt'],
        'title': f"{r['d']}<br>强度: {r['w']}<br>来源: {', '.join(r['segs'])}",
        'value': max(r['w'] or 1, 1),
    }
    for r in relationships
]

# 社区面板数据（图级层次，标注跨视频情况）
def media_of(segment_ids):
    medias = set()
    for sid in segment_ids:
        parts = sid.split('_segment_')
        if parts:
            medias.add(parts[0])
    return sorted(medias)

comm_panel = []
for c in sorted(graph_communities, key=lambda x: (x.level, -x.entity_count)):
    medias = media_of(c.source_segments)
    comm_panel.append({
        'id': c.id, 'level': c.level, 'parent': c.parent_id,
        'count': c.entity_count,
        'summary': ', '.join(e.name for e in c.entities[:10]) + ('...' if c.entity_count > 10 else ''),
        'medias': medias,
        'color': color_map.get(c.id) if c.level == 0 else None,
    })

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>知识图谱 - Neo4j 多视频</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
body {{ font-family: Arial, sans-serif; margin: 0; display: flex; height: 100vh; background: #f5f5f5; }}
#sidebar {{ width: 340px; overflow-y: auto; background: white; padding: 15px; box-shadow: 2px 0 5px rgba(0,0,0,0.1); }}
#graph {{ flex: 1; }}
.stats {{ background: #eef; padding: 10px; border-radius: 5px; margin-bottom: 15px; font-size: 13px; }}
.comm {{ margin-bottom: 10px; padding: 8px; border-radius: 5px; background: #fafafa; border-left: 4px solid #ccc; font-size: 12px; }}
.comm h4 {{ margin: 0 0 4px 0; font-size: 13px; }}
.comm p {{ margin: 2px 0; color: #555; }}
h2 {{ font-size: 16px; }}
</style>
</head>
<body>
<div id="sidebar">
  <h2>知识图谱（Neo4j）</h2>
  <div class="stats">
    视频: {media_count} | 片段: {segment_count} | 实体: {len(nodes)} | 关系: {len(edges)} | 图级社区: {len(comm_panel)}
  </div>
  <h2>图级社区（跨视频 Leiden 层次）</h2>
  <div id="communities"></div>
</div>
<div id="graph"></div>
<script>
const nodes = new vis.DataSet({json.dumps(nodes, ensure_ascii=False)});
const edges = new vis.DataSet({json.dumps(edges, ensure_ascii=False)});
const comms = {json.dumps(comm_panel, ensure_ascii=False)};

const container = document.getElementById('graph');
new vis.Network(container, {{nodes, edges}}, {{
  nodes: {{ shape: 'dot', scaling: {{min: 8, max: 30}}, font: {{size: 13}} }},
  edges: {{ color: '#bbb', smooth: true, scaling: {{min: 1, max: 4}} }},
  physics: {{ barnesHut: {{ gravitationalConstant: -4000, springLength: 120 }} }},
  interaction: {{ hover: true, tooltipDelay: 100 }}
}});

const panel = document.getElementById('communities');
comms.forEach(c => {{
  const div = document.createElement('div');
  div.className = 'comm';
  div.style.marginLeft = (c.level * 16) + 'px';
  if (c.color) div.style.borderLeftColor = c.color;
  const crossTag = c.medias.length > 1 ? `<b style="color:#d32f2f">跨视频: ${{c.medias.join(', ')}}</b><br>` : `[${{c.medias[0] || ''}}] `;
  div.innerHTML = `<h4>${{c.id}} (${{c.count}} 实体, L${{c.level}})</h4><p>${{crossTag}}${{c.summary}}</p>`;
  panel.appendChild(div);
}});
</script>
</body>
</html>"""

with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"导出完成: {OUTPUT}")
print(f"实体 {len(nodes)}, 关系 {len(edges)}, 社区 {len(communities)}")
