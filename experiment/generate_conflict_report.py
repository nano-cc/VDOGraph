#!/usr/bin/env python3
"""
生成关系冲突检测报告
"""

import json

def main():
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_results.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    stats = data['statistics']
    canonical_relationships = data['canonical_relationships']
    duplicate_groups = data['duplicate_groups']

    report = []
    report.append("# 关系冲突检测报告\n")
    report.append("## 统计概览\n")
    report.append(f"- **总关系数（抽取）**: {stats['total']}\n")
    report.append(f"- **标准关系数（去重后）**: {stats['active']}\n")
    report.append(f"- **重复关系数**: {stats['duplicates']}\n")
    report.append(f"- **重复率**: {stats['duplicates'] / stats['total']:.1%}\n" if stats['total'] > 0 else "- **重复率**: 0%\n")
    report.append("\n---\n")

    # 重复关系
    if duplicate_groups:
        report.append(f"\n## 重复的关系（{len(duplicate_groups)} 组）\n")
        for group in duplicate_groups:
            canonical = group['canonical_rel']
            report.append(f"### {canonical['source_entity_name']} -> {canonical['target_entity_name']}\n")
            report.append(f"- **描述**: {canonical['description']}\n")
            report.append(f"- **强度**: {canonical['strength']}\n")
            report.append(f"- **来源片段数**: {canonical['source_count']}\n")
            report.append(f"- **重复次数**: {len(group['duplicates'])}\n")
            report.append("\n")
    else:
        report.append("\n## 重复的关系\n")
        report.append("*（未发现重复关系）*\n")

    # 所有标准关系
    report.append(f"\n## 所有标准关系（{len(canonical_relationships)} 个）\n")
    report.append("| 序号 | 源实体 | 目标实体 | 描述 | 强度 | 来源片段数 |\n")
    report.append("|------|-------|---------|------|------|------------|\n")

    for idx, rel in enumerate(canonical_relationships, 1):
        report.append(f"| {idx} | {rel['source_entity_name']} | {rel['target_entity_name']} | {rel['description'][:50]}... | {rel['strength']} | {rel['source_count']} |\n")

    # 保存报告
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_report.md'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(''.join(report))

    print(f"✅ 冲突检测报告已生成: {output_file}")

if __name__ == "__main__":
    main()
