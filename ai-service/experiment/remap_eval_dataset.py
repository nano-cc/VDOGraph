#!/usr/bin/env python3
"""评测集标注重映射：旧 60s 网格段号 → 新静音分片段号（BENCH 账号）

旧标注 expected_segment_indexes 的段号 i 对应旧网格 [60000i, 60000(i+1)) ms。
新图（bench 账号，静音感知分片）从 Neo4j 读各 media 的 Segment(start_ms,end_ms)，
按时间重叠率把旧段号映射到新段号。重叠率 <50% 的标注打印出来人工复核。

产出：eval_dataset_bench.json
用法：python experiment/remap_eval_dataset.py
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')
from neo4j import GraphDatabase

ROOT = '/mnt/Data/projs/Java/DOVideo-AI/ai-service'
SRC = f'{ROOT}/eval_dataset.json'
DST = f'{ROOT}/eval_dataset_bench.json'
BENCH_USER_ID = 4
OLD_GRID_MS = 60000

MYSQL_PW = os.environ.get('MYSQL_ROOT_PASSWORD', 'ExJNOyukK9iO0IA0TXf8Y3QF')


def bench_media_map():
    """视频名 -> bench 账号 media_id"""
    out = subprocess.run(
        ['docker', 'exec', 'dovideo-ai-mysql-1', 'mysql', '-u', 'root',
         f'-p{MYSQL_PW}', '--default-character-set=utf8mb4', 'media_db', '-N',
         '-e', f'SELECT id, filename FROM media_files WHERE user_id={BENCH_USER_ID};'],
        capture_output=True, text=True)
    mapping = {}
    for line in out.stdout.strip().split('\n'):
        parts = line.split('\t')
        if len(parts) == 2:
            mid, fname = parts
            mapping[fname.replace('.mp4', '').replace('.MP4', '')] = int(mid)

    def find(video_name):
        if video_name in mapping:
            return mapping[video_name]
        for fname, mid in mapping.items():
            if fname.startswith(video_name):
                return mid
        return None
    return find


def new_segments(driver, media_id):
    """bench 新图的分片区间：{新段号: (start_ms, end_ms)}"""
    with driver.session() as s:
        rows = s.run(
            "MATCH (sg:Segment) WHERE sg.id STARTS WITH $prefix "
            "RETURN sg.id AS id, sg.start_ms AS st, sg.end_ms AS en",
            prefix=f'media_{media_id}_segment_').data()
    segs = {}
    for r in rows:
        idx = int(r['id'].rsplit('_', 1)[1])
        segs[idx] = (r['st'], r['en'])
    return segs


def remap_index(old_idx, segs):
    """旧段号 -> (新段号, 重叠率)。旧段 = [60000*old_idx, 60000*(old_idx+1))"""
    lo, hi = old_idx * OLD_GRID_MS, (old_idx + 1) * OLD_GRID_MS
    best, best_ratio = None, 0.0
    for idx, (st, en) in segs.items():
        ov = max(0, min(hi, en) - max(lo, st))
        ratio = ov / (hi - lo)
        if ratio > best_ratio:
            best, best_ratio = idx, ratio
    return best, best_ratio


def main():
    dataset = json.load(open(SRC))
    questions = dataset if isinstance(dataset, list) else dataset.get('questions', dataset)

    neo4j_pw = os.environ.get('NEO4J_PASSWORD', '')
    if not neo4j_pw:
        for line in open('/mnt/Data/projs/Java/DOVideo-AI/.env'):
            if line.startswith('NEO4J_PASSWORD'):
                neo4j_pw = line.split('=', 1)[1].strip()
    driver = GraphDatabase.driver('bolt://localhost:7687', auth=('neo4j', neo4j_pw))

    find = bench_media_map()
    seg_cache = {}
    review = []

    def segs_of(video):
        mid = find(video)
        if mid is None:
            raise ValueError(f'视频 {video} 在 bench 账号下找不到')
        if mid not in seg_cache:
            seg_cache[mid] = new_segments(driver, mid)
            print(f'  {video} (media {mid}): {len(seg_cache[mid])} 段 '
                  f'总时长 {max(e for _, e in seg_cache[mid].values())/1000:.0f}s')
        return seg_cache[mid]

    n_remapped = 0
    for q in questions:
        t = q.get('type')
        if t == 'oos':
            continue
        if t == 'cross':
            for exp in q['expected_segment_indexes']:
                segs = segs_of(exp['video'])
                new_idx = []
                for oi in exp['indexes']:
                    ni, ratio = remap_index(oi, segs)
                    n_remapped += 1
                    if ratio < 0.5:
                        review.append(f"题{q['id']} {exp['video']} 旧段{oi}->新段{ni} 重叠率{ratio:.0%}")
                    if ni is not None and ni not in new_idx:
                        new_idx.append(ni)
                exp['indexes'] = sorted(new_idx)
        else:
            segs = segs_of(q['video'])
            new_idx = []
            for oi in q['expected_segment_indexes']:
                ni, ratio = remap_index(oi, segs)
                n_remapped += 1
                if ratio < 0.5:
                    review.append(f"题{q['id']} {q['video']} 旧段{oi}->新段{ni} 重叠率{ratio:.0%}")
                if ni is not None and ni not in new_idx:
                    new_idx.append(ni)
            q['expected_segment_indexes'] = sorted(new_idx)

    driver.close()
    json.dump(questions, open(DST, 'w'), ensure_ascii=False, indent=2)
    print(f'\n✅ 重映射完成：{n_remapped} 处段号 -> {DST}')
    if review:
        print(f'\n⚠️ {len(review)} 处重叠率 <50%，建议人工复核：')
        for r in review:
            print('  ' + r)
    else:
        print('全部重叠率 ≥50%，无需复核')


if __name__ == '__main__':
    main()
