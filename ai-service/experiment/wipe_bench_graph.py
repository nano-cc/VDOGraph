#!/usr/bin/env python3
"""清空 bench 图谱（保留片段转写）：为中文 prompt 重抽做准备
删除：user_4 的 Entity/RELATES_TO/Community/BELONGS_TO + Segment.raw_extraction
保留：Media/Segment 节点及 transcript/ocr/帧（ASR 不重跑）
用法：python experiment/wipe_bench_graph.py [group_id]
"""
import sys
sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')
from neo4j import GraphDatabase

GROUP = sys.argv[1] if len(sys.argv) > 1 else 'user_4'

d = GraphDatabase.driver('bolt://localhost:7687', auth=('neo4j', 'password'))
with d.session() as s:
    r = s.run("MATCH (e:Entity {group_id: $g}) DETACH DELETE e RETURN count(*) AS n", g=GROUP).single()
    print(f"删除实体: {r['n']}")
    r = s.run("MATCH (c:Community {group_id: $g}) DETACH DELETE c RETURN count(*) AS n", g=GROUP).single()
    print(f"删除社区: {r['n']}")
    r = s.run("""
        MATCH (sg:Segment {group_id: $g})
        REMOVE sg.raw_extraction
        RETURN count(*) AS n
    """, g=GROUP).single()
    print(f"清除抽取缓存: {r['n']}")
d.close()
print("✅ bench 图谱已清空（转写保留），可重投 analyze+commit")
