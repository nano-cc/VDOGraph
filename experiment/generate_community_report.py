#!/usr/bin/env python3
"""
生成社区检测报告
"""

import json

def main():
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/community_detection_results.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    stats = data['statistics']
    communities = data['communities']

    report = []
    report.append("# 社区检测报告\n")
    report.append("## 统计概览\n")
    report.append(f"- **总社区数**: {stats['total_communities']}\n")
    report.append(f"- **总实体数**: {stats['total_entities']}\n")
    report.append(f"- **总关系数**: {stats['total_relationships']}\n")
    report.append(f"- **平均社区大小**: {stats['avg_community_size']:.1f}\n")
    report.append("\n---\n")

    # 社区详情
    report.append("\n## 社区详情\n")

    for idx, community in enumerate(communities, 1):
        report.append(f"\n### 社区 {idx}: {community['id']}\n")
        report.append(f"- **实体数**: {community['entity_count']}\n")
        report.append(f"- **关系数**: {community['relationship_count']}\n")
        report.append(f"- **来源片段数**: {len(community['source_segments'])}\n")
        report.append(f"- **摘要**: {community['summary']}\n")

        # 实体列表
        report.append(f"\n#### 实体列表（{community['entity_count']} 个）\n")
        report.append("| 序号 | 实体名称 | 类型 | 来源片段数 |\n")
        report.append("|------|---------|------|------------|\n")
        for e_idx, entity in enumerate(community['entities'], 1):
            report.append(f"| {e_idx} | {entity['name']} | {entity['type']} | {entity['source_count']} |\n")

        # 关系列表
        if community['relationships']:
            report.append(f"\n#### 关系列表（{community['relationship_count']} 个）\n")
            report.append("| 序号 | 源实体 | 目标实体 | 描述 | 强度 |\n")
            report.append("|------|-------|---------|------|------|\n")
            for r_idx, rel in enumerate(community['relationships'], 1):
                report.append(f"| {r_idx} | {rel['source']} | {rel['target']} | {rel['description'][:50]}... | {rel['strength']} |\n")

        # 追溯信息
        report.append(f"\n#### 追溯信息\n")
        report.append(f"- **来源片段**: {', '.join(community['source_segments'])}\n")

        report.append("\n---\n")

    # 保存报告
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/community_detection_report.md'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(''.join(report))

    print(f"✅ 社区检测报告已生成: {output_file}")

if __name__ == "__main__":
    main()
