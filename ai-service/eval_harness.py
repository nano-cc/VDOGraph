#!/usr/bin/env python3
"""
L1 检索层评测 harness（完整版）
指标：Hit@k、MRR、P@k、R@k、F1@k、macro 平均、分桶、跨视频污染率
产出：eval/reports/{timestamp}/l1_report.json + 终端摘要
对比：--baseline eval/baseline.json 输出与基线的差异
"""
import sys
sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')

import json
import asyncio
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict
from app.services.graph_retriever import GraphRetriever

DATASET_PATH = '/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval_dataset.json'
REPORT_DIR = Path('/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval/reports')
BASELINE_PATH = Path('/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval/baseline.json')


def load_video_media_map() -> Dict:
    """视频文件名 -> media_id 映射（MySQL filename 最权威）"""
    import subprocess
    import os
    root_pw = os.environ.get('MYSQL_ROOT_PASSWORD', '')
    out = subprocess.run(
        ['docker', 'exec', 'dovideo-ai-mysql-1', 'mysql', '-u', 'root',
         f'-p{root_pw}', '--default-character-set=utf8mb4', 'media_db',
         '-e', 'SELECT id, filename FROM media_files;'],
        capture_output=True, text=True
    )
    mapping = {}
    for line in out.stdout.strip().split('\n')[1:]:
        parts = line.split('\t')
        if len(parts) == 2:
            mid, fname = parts
            for suffix in ('.mp4', '.MP4'):
                fname = fname.replace(suffix, '')
            mapping[fname] = int(mid)

    def find(video_name: str):
        if video_name in mapping:
            return mapping[video_name]
        for fname, mid in mapping.items():
            if fname.startswith(video_name):
                return mid
        return None
    mapping['_find'] = find
    return mapping


async def eval_question(retriever: GraphRetriever, q: Dict, media_map: Dict, top_k: int) -> Dict:
    qtype = q['type']
    if qtype == 'oos':
        return {'id': q['id'], 'type': qtype, 'skipped': True}

    if qtype == 'global':
        result = await retriever.global_search(q['question'], GROUP_ID, top_k=top_k)
    elif qtype == 'segment':
        result = await retriever.segment_search(q['question'], GROUP_ID, top_k=top_k)
    else:  # local / cross
        result = await retriever.local_search(q['question'], GROUP_ID, top_k=top_k)

    retrieved_sids = [c['segment_id'] for c in result['citations']]

    find = media_map['_find']
    if qtype == 'cross':
        expected_sids = set()
        for exp in q['expected_segment_indexes']:
            mid = find(exp['video'])
            if mid is not None:
                expected_sids.update(f"media_{mid}_segment_{i}" for i in exp['indexes'])
    else:
        mid = find(q['video'])
        expected_sids = {f"media_{mid}_segment_{i}" for i in q['expected_segment_indexes']} if mid is not None else set()

    retrieved_topk = retrieved_sids[:top_k]
    hits = [sid for sid in retrieved_topk if sid in expected_sids]

    hit_rank = None
    for rank, sid in enumerate(retrieved_sids, 1):
        if sid in expected_sids:
            hit_rank = rank
            break

    tp = len(hits)
    precision = tp / len(retrieved_topk) if retrieved_topk else 0
    recall = tp / len(expected_sids) if expected_sids else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    expected_media_ids = {int(s.split('_')[1]) for s in expected_sids} if expected_sids else set()
    pollution = sum(1 for sid in retrieved_topk
                    if expected_media_ids and int(sid.split('_')[1]) not in expected_media_ids) / max(len(retrieved_topk), 1)

    return {
        'id': q['id'], 'type': qtype, 'question': q['question'],
        'hit': hit_rank is not None, 'rank': hit_rank,
        'precision': precision, 'recall': recall, 'f1': f1,
        'pollution': pollution,
        'retrieved': retrieved_topk, 'expected': sorted(expected_sids),
    }


def summarize(results: List[Dict], top_k: int) -> Dict:
    valid = [r for r in results if not r.get('skipped')]
    if not valid:
        return {}

    def avg(key):
        return sum(r[key] for r in valid) / len(valid)

    summary = {
        'total': len(valid),
        f'hit_at_{top_k}': sum(1 for r in valid if r['hit']) / len(valid),
        'mrr': sum(1.0 / r['rank'] for r in valid if r['hit']) / len(valid),
        'macro_precision': avg('precision'),
        'macro_recall': avg('recall'),
        'macro_f1': avg('f1'),
        'avg_pollution': avg('pollution'),
        'by_type': {}
    }
    by_type = {}
    for r in valid:
        by_type.setdefault(r['type'], []).append(r)
    for t, rs in sorted(by_type.items()):
        summary['by_type'][t] = {
            'count': len(rs),
            f'hit_at_{top_k}': sum(1 for r in rs if r['hit']) / len(rs),
            'mrr': sum(1.0 / r['rank'] for r in rs if r['hit']) / len(rs),
            'macro_f1': sum(r['f1'] for r in rs) / len(rs),
        }
    return summary


def print_report(summary: Dict, failed: List[Dict], top_k: int):
    print(f"\n{'='*64}")
    print(f"总题数: {summary['total']} | Hit@{top_k}: {summary[f'hit_at_{top_k}']:.1%} | MRR: {summary['mrr']:.3f}")
    print(f"Macro P: {summary['macro_precision']:.3f} | Macro R: {summary['macro_recall']:.3f} | Macro F1: {summary['macro_f1']:.3f} | 污染率: {summary['avg_pollution']:.1%}")
    print(f"\n分桶:")
    for t, s in summary['by_type'].items():
        print(f"  {t}: {s['count']} 题, Hit@{top_k}={s[f'hit_at_{top_k}']:.1%}, MRR={s['mrr']:.3f}, F1={s['macro_f1']:.3f}")
    if failed:
        print(f"\n未命中题目 ({len(failed)}):")
        for r in failed:
            print(f"  ❌ {r['id']}: {r['question'][:45]}")


def diff_baseline(summary: Dict, top_k: int):
    """与基线对比"""
    if not BASELINE_PATH.exists():
        return
    with open(BASELINE_PATH) as f:
        baseline = json.load(f)['l1']
    print(f"\n{'='*64}\n与基线对比:")
    for key in [f'hit_at_{top_k}', 'mrr', 'macro_precision', 'macro_recall', 'macro_f1', 'avg_pollution']:
        old = baseline.get(key, 0)
        new = summary.get(key, 0)
        delta = new - old
        mark = '📈' if (delta > 0) != (key == 'avg_pollution') else ('📉' if delta != 0 else '➖')
        print(f"  {mark} {key}: {old:.3f} → {new:.3f} ({delta:+.3f})")


async def run_eval(dataset_path: str, top_k: int, save: bool, update_baseline: bool, use_rerank: bool = True):
    with open(dataset_path) as f:
        dataset = json.load(f)

    media_map = load_video_media_map()
    print(f"视频映射: {len(media_map)-1} 个视频")

    retriever = GraphRetriever(use_rerank=use_rerank)
    results = []
    for q in dataset:
        r = await eval_question(retriever, q, media_map, top_k)
        results.append(r)
        if not r.get('skipped'):
            mark = '✅' if r['hit'] else '❌'
            print(f"{mark} [{r['type']}] {r['id']}: {q['question'][:40]} (rank={r['rank']})")

    summary = summarize(results, top_k)
    failed = [r for r in results if not r.get('skipped') and not r['hit']]
    print_report(summary, failed, top_k)
    diff_baseline(summary, top_k)

    if save:
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        report_dir = REPORT_DIR / ts
        report_dir.mkdir(parents=True, exist_ok=True)
        report = {'timestamp': ts, 'top_k': top_k, 'summary': summary, 'details': results}
        (report_dir / 'l1_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n报告已保存: {report_dir / 'l1_report.json'}")

    if update_baseline:
        BASELINE_PATH.parent.mkdir(exist_ok=True)
        baseline = {}
        if BASELINE_PATH.exists():
            baseline = json.loads(BASELINE_PATH.read_text())
        baseline['l1'] = summary
        BASELINE_PATH.write_text(json.dumps(baseline, ensure_ascii=False, indent=2))
        print(f"基线已更新: {BASELINE_PATH}")


# 三期后检索强制 group_id；eval 数据集基于历史测试用户 kgtest(id=2)
GROUP_ID = 'user_2'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', default=DATASET_PATH)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--save', action='store_true')
    parser.add_argument('--update-baseline', action='store_true')
    parser.add_argument('--no-rerank', action='store_true', help='禁用 BGE 精排 (A/B 对比)')
    args = parser.parse_args()
    asyncio.run(run_eval(args.dataset, args.top_k, args.save, args.update_baseline, not args.no_rerank))
