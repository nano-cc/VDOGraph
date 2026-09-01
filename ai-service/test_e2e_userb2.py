#!/usr/bin/env python3
"""三期 E2E：user B 上传片段 → 验证 group 隔离"""
import requests, xxhash, os, sys, subprocess

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024

def qh(path):
    h = xxhash.xxh64()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def upload(token, src, offset, out):
    subprocess.run(['ffmpeg', '-y', '-ss', str(offset), '-t', '40', '-i', src,
                    '-c:v', 'libx264', '-c:a', 'aac', out], capture_output=True, check=True)
    size = os.path.getsize(out)
    total = (size + CHUNK - 1) // CHUNK
    hash_ = qh(out)
    headers = {'Authorization': f'Bearer {token}'}
    r = requests.post(f'{BASE}/media/v2/init-upload',
                      params={'filename': os.path.basename(out), 'size': size, 'totalChunks': total, 'quickHash': hash_},
                      headers=headers)
    r.raise_for_status()
    d = r.json()['data']
    if d['status'] == 'INSTANT':
        return d['mediaId']
    uid = d['uploadId']
    with open(out, 'rb') as f:
        for i in range(1, total + 1):
            f.seek((i - 1) * CHUNK)
            r = requests.post(f'{BASE}/media/v2/presign',
                              params={'uploadId': uid, 'partNumbers': [i]}, headers=headers)
            requests.put(r.json()['data'][str(i)], data=f.read(CHUNK)).raise_for_status()
    r = requests.post(f'{BASE}/media/v2/complete-upload', params={'uploadId': uid, 'quickHash': hash_}, headers=headers)
    r.raise_for_status()
    return r.json()['data']['mediaId']

if __name__ == '__main__':
    token = open('/tmp/token2.txt').read().strip()
    media_id = upload(token, sys.argv[1], int(sys.argv[2]), '/tmp/userB_clip2.mp4')
    print(f'user B 上传完成 mediaId={media_id}')
