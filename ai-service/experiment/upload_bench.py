#!/usr/bin/env python3
"""BENCH 账号批量上传评测集 7 个视频（v2 S3 直传，走真实全链路）"""
import os
import sys
import time
import requests
import xxhash

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024
VIDEO_DIR = '/mnt/Data/linux-home/cong/data/视频'
FILES = [
    '波士顿圆脸.mp4',
    '波士顿圆脸2.mp4',
    '中国人牛肉自由.mp4',
    '历史.mp4',
    '历史2-视频形式带音乐ppt展示.mp4',
    'LLM到Skills.mp4',
    '产品评测.mp4',
]


def login():
    r = requests.post(f'{BASE}/user/login', json={'username': 'bench', 'password': 'bench123456'})
    r.raise_for_status()
    return {'Authorization': 'Bearer ' + r.json()['data']['token']}


def quick_hash(path):
    h = xxhash.xxh3_128()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def upload_one(path, headers):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    qh = quick_hash(path)
    t0 = time.time()

    r = requests.post(f'{BASE}/media/v2/init-upload',
                      params={'filename': name, 'size': size, 'totalChunks': total, 'quickHash': qh},
                      headers=headers)
    r.raise_for_status()
    d = r.json()['data']
    if d['status'] == 'INSTANT':
        print(f"[{name}] INSTANT 秒传 mediaId={d['mediaId']}", flush=True)
        return d['mediaId']

    upload_id = d['uploadId']
    with open(path, 'rb') as f:
        for i in range(1, total + 1):
            f.seek((i - 1) * CHUNK)
            blob = f.read(CHUNK)
            r = requests.post(f'{BASE}/media/v2/presign',
                              params={'uploadId': upload_id, 'partNumbers': i}, headers=headers)
            url = r.json()['data'][str(i)]
            r = requests.put(url, data=blob)
            r.raise_for_status()

    r = requests.post(f'{BASE}/media/v2/complete-upload',
                      params={'uploadId': upload_id, 'quickHash': qh}, headers=headers)
    r.raise_for_status()
    media_id = r.json()['data']['mediaId']
    print(f"[{name}] ✅ mediaId={media_id} parts={total} 上传{time.time()-t0:.1f}s", flush=True)
    return media_id


def main():
    headers = login()
    results = {}
    for fname in FILES:
        path = os.path.join(VIDEO_DIR, fname)
        try:
            mid = upload_one(path, headers)
            results[fname] = mid
        except Exception as e:
            print(f"[{fname}] ❌ {e}", flush=True)
            results[fname] = None
    print("\n==== 上传结果 ====")
    for f, m in results.items():
        print(f"  {f} -> mediaId={m}")


if __name__ == '__main__':
    main()
