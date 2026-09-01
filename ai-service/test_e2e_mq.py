#!/usr/bin/env python3
"""E2E：真实上传 → triggerBuild → MQ → consumer → Python 后台构建"""
import requests, xxhash, os, sys, subprocess

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024

def login():
    r = requests.post(f'{BASE}/user/login', json={'username': 'kgtest', 'password': 'test123456'})
    return {'Authorization': 'Bearer ' + r.json()['data']['token']}

def qh(path):
    h = xxhash.xxh3_128()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def main():
    src = sys.argv[1]
    path = '/tmp/e2e_mq_clip2.mp4'
    subprocess.run(['ffmpeg', '-y', '-ss', '420', '-t', '40', '-i', src,
                    '-c:v', 'libx264', '-c:a', 'aac', path], capture_output=True, check=True)
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    hash_ = qh(path)
    headers = login()

    r = requests.post(f'{BASE}/media/v2/init-upload',
                      params={'filename': 'e2e_mq_clip2.mp4', 'size': size, 'totalChunks': total, 'quickHash': hash_},
                      headers=headers)
    r.raise_for_status()
    d = r.json()['data']
    if d['status'] == 'INSTANT':
        print('INSTANT 秒传命中（不应发生，换了新偏移）'); sys.exit(1)
    uid = d['uploadId']
    with open(path, 'rb') as f:
        for i in range(1, total + 1):
            f.seek((i - 1) * CHUNK)
            r = requests.post(f'{BASE}/media/v2/presign',
                              params={'uploadId': uid, 'partNumbers': [i]}, headers=headers)
            url = r.json()['data'][str(i)]
            requests.put(url, data=f.read(CHUNK)).raise_for_status()
    r = requests.post(f'{BASE}/media/v2/complete-upload', params={'uploadId': uid, 'quickHash': hash_}, headers=headers)
    r.raise_for_status()
    media_id = r.json()['data']['mediaId']
    print(f'上传完成 mediaId={media_id}，观察 kg:task:{media_id} 状态机...')

if __name__ == '__main__':
    main()
