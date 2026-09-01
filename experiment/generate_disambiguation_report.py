#!/usr/bin/env python3
"""
生成实体消歧报告
"""

import json

def main():
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    stats = data['statistics']
    canonical_entities = data['canonical_entities']

    report = []
    report.append("# 实体消歧报告\n")
    report.append("## 统计概览\n")
    report.append(f"- **总实体数（抽取）**: {stats['total']}\n")
    report.append(f"- **标准实体数（消歧后）**: {stats['canonical_count']}\n")
    report.append(f"- **合并的实体数**: {stats['total'] - stats['canonical_count']}\n")
    report.append(f"- **合并率**: {stats['merge_rate']:.1%}\n")
    report.append(f"- **Level 1 命中（精确匹配）**: {stats['level1_hits']}\n")
    report.append(f"- **Level 2 命中（模糊匹配）**: {stats['level2_hits']}\n")
    report.append(f"- **Level 3 命中（LLM 判断）**: {stats['level3_hits']}\n")
    report.append(f"- **新实体数**: {stats['new_entities']}\n")
    report.append("\n---\n")

    # 合并的实体
    merged = [e for e in canonical_entities if e['source_count'] > 1]
    report.append(f"\n## 合并的实体（{len(merged)} 个）\n")
    report.append("这些实体在多个片段中出现，已被合并：\n\n")

    for e in merged:
        report.append(f"### {e['name']} ({e['type']})\n")
        report.append(f"- **来源片段数**: {e['source_count']}\n")
        report.append(f"- **来源片段**: {', '.join([s['segment_id'] for s in e['sources']])}\n")
        if e['aliases']:
            report.append(f"- **别名**: {', '.join(e['aliases'])}\n")
        report.append(f"- **描述**: {e['description']}\n")
        report.append("\n")

    # 未合并的实体（疑似应该合并但未合并）
    report.append("\n## 疑似应该合并但未合并的实体\n")

    # 手动检查一些可能应该合并的
    suspicious_pairs = [
        ('澳大利亚', '澳洲'),
        ('国家', '中国'),
    ]

    report.append("以下实体可能应该合并，但由于相似度低于阈值未合并：\n\n")
    for name1, name2 in suspicious_pairs:
        e1 = next((e for e in canonical_entities if e['name'] == name1), None)
        e2 = next((e for e in canonical_entities if e['name'] == name2), None)
        if e1 and e2:
            report.append(f"- **{name1}** vs **{name2}**\n")
            report.append(f"  - {name1}: {e1['source_count']} 个片段\n")
            report.append(f"  - {name2}: {e2['source_count']} 个片段\n")

    # 所有标准实体列表
    report.append(f"\n## 所有标准实体（{len(canonical_entities)} 个）\n")
    report.append("| 序号 | 实体名称 | 类型 | 来源片段数 | 别名 |\n")
    report.append("|------|---------|------|-----------|------|\n")

    for idx, e in enumerate(canonical_entities, 1):
        aliases = ', '.join(e['aliases']) if e['aliases'] else '-'
        report.append(f"| {idx} | {e['name']} | {e['type']} | {e['source_count']} | {aliases} |\n")

    # 保存报告
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_report.md'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(''.join(report))

    print(f"✅ 消歧报告已生成: {output_file}")

if __name__ == "__main__":
    main()
