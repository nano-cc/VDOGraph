#!/usr/bin/env python3
"""上传指定视频文件走 v2 S3 直传链路（用于两跳全链路验证）"""
import os
import sys
import requests
import xxhash

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024


def login():
    r = requests.post(f'{BASE}/user/login', json={'username': 'kgtest', 'password': 'test123456'})
    return {'Authorization': 'Bearer ' + r.json()['data']['token']}


def quick_hash(path):
    h = xxhash.xxh3_128()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def main():
    path = sys.argv[1]
    name = os.path.basename(path)
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    qh = quick_hash(path)
    headers = login()

    r = requests.post(f'{BASE}/media/v2/init-upload',
                      params={'filename': name, 'size': size, 'totalChunks': total, 'quickHash': qh},
                      headers=headers)
    r.raise_for_status()
    d = r.json()['data']
    if d['status'] == 'INSTANT':
        print(f"INSTANT 秒传 mediaId={d['mediaId']}（文件已存在）")
        return d['mediaId']
    upload_id = d['uploadId']
    print(f"init: uploadId={upload_id} totalChunks={total}")

    with open(path, 'rb') as f:
        for i in range(1, total + 1):
            f.seek((i - 1) * CHUNK)
            blob = f.read(CHUNK)
            r = requests.post(f'{BASE}/media/v2/presign',
                              params={'uploadId': upload_id, 'partNumbers': i}, headers=headers)
            url = r.json()['data'][str(i)]
            r = requests.put(url, data=blob)
            r.raise_for_status()
            print(f"  part {i}/{total} ✓")

    r = requests.post(f'{BASE}/media/v2/complete-upload',
                      params={'uploadId': upload_id, 'quickHash': qh}, headers=headers)
    r.raise_for_status()
    media_id = r.json()['data']['mediaId']
    print(f"✅ 上传完成 mediaId={media_id}（KG 构建已触发）")
    return media_id


if __name__ == '__main__':
    main()
