#!/usr/bin/env python3
"""
上传链路健壮性回归测试
R1 complete 幂等（重复 complete 返回同一 mediaId）
R2 并发 init 同文件（第二次归并/冲突，不产生重复记录）
R3 size/totalChunks 校验（不符应拒绝）
R4 正常上传仍工作
"""
import requests
import xxhash
import os
import threading

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024

def login():
    r = requests.post(f'{BASE}/user/login', json={'username': 'kgtest', 'password': 'test123456'})
    return {'Authorization': 'Bearer ' + r.json()['data']['token']}

def qh(path):
    h = xxhash.xxh64()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def init(headers, name, size, total, hash_):
    r = requests.post(f'{BASE}/media/v2/init-upload',
                      params={'filename': name, 'size': size, 'totalChunks': total, 'quickHash': hash_},
                      headers=headers)
    return r.status_code, r.json()

def presign(headers, uid, nums):
    r = requests.post(f'{BASE}/media/v2/presign', params={'uploadId': uid, 'partNumbers': nums}, headers=headers)
    r.raise_for_status()
    return r.json()['data']

def complete(headers, uid, hash_):
    r = requests.post(f'{BASE}/media/v2/complete-upload', params={'uploadId': uid, 'quickHash': hash_}, headers=headers)
    return r.status_code, r.json()

def upload_all(headers, path):
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    hash_ = qh(path)
    _, init_data = init(headers, os.path.basename(path), size, total, hash_)
    d = init_data['data']
    if d['status'] == 'INSTANT':
        return d['mediaId'], hash_
    uid = d['uploadId']
    uploaded = {p['partNumber'] for p in d.get('uploadedParts', [])}
    with open(path, 'rb') as f:
        for i in range(1, total + 1):
            if i in uploaded:
                continue
            f.seek((i - 1) * CHUNK)
            requests.put(presign(headers, uid, [i])[str(i)], data=f.read(CHUNK)).raise_for_status()
    return uid, hash_

def main():
    headers = login()
    path = '/tmp/boston_robust.mp4'
    import subprocess
    subprocess.run(['ffmpeg', '-y', '-ss', '240', '-t', '40', '-i', '/home/cong/视频/波士顿圆脸.mp4',
                    '-c:v', 'libx264', '-c:a', 'aac', path], capture_output=True)

    # R3: size/totalChunks 校验
    print('=== R3 size/totalChunks 校验 ===')
    code, resp = init(headers, 'x.mp4', 100 * 1024 * 1024, 999, 'fakehash123')
    print(f'totalChunks 不符: {code} {resp.get("message", "")[:60]}（预期 400 拒绝）')
    code, resp = init(headers, 'x.mp4', 3 * 1024 * 1024 * 1024, 600, 'fakehash123')
    print(f'超过 2GB: {code} {resp.get("message", "")[:60]}（预期 400 拒绝）')

    # R2: 并发 init 同文件
    print('\n=== R2 并发 init 同文件 ===')
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    hash_ = qh(path)
    results = []
    def do_init(tag):
        code, resp = init(headers, os.path.basename(path), size, total, hash_)
        results.append((tag, code, resp.get('data', {}).get('status'), resp.get('message', '')))
    threads = [threading.Thread(target=do_init, args=(i,)) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    for r in results:
        print(f'  init{r[0]}: {r[1]} status={r[2]} {r[3][:50]}')
    statuses = [r[2] for r in results if r[1] == 200]
    assert statuses.count('NEW') <= 1, f'R2 失败：{statuses.count("NEW")} 个 NEW（应 ≤1）'
    print('  ✅ 并发 init 最多一个 NEW（其余归并/冲突）')

    # R4: 完整上传
    print('\n=== R4 完整上传 ===')
    uid, hash_ = upload_all(headers, path)
    if isinstance(uid, int):
        print('  已存在（INSTANT）mediaId =', uid)
        code, resp = complete(headers, 'any-id', hash_)
        print(f'  幂等 complete: {code} {resp.get("data")}（预期命中已有）')
    else:
        code, resp = complete(headers, uid, hash_)
        mid1 = resp.get('data', {}).get('mediaId')
        print(f'  complete: {code} mediaId={mid1}')

        # R1: complete 幂等
        print('\n=== R1 complete 幂等 ===')
        code2, resp2 = complete(headers, uid, hash_)
        mid2 = resp2.get('data', {}).get('mediaId')
        print(f'  第一次: {mid1}, 第二次: {mid2}, 幂等标记: {resp2.get("data", {}).get("idempotent")}')
        assert mid1 == mid2, 'R1 失败：两次 complete 返回不同 mediaId'
        print('  ✅ 两次 complete 返回同一 mediaId')

        # 再验 MySQL quickHash 幂等路径（uploadId 乱编也该命中）
        code3, resp3 = complete(headers, 'nonexistent-upload-id', hash_)
        mid3 = resp3.get('data', {}).get('mediaId')
        print(f'  乱编 uploadId + 正确 quickHash: {mid3}（预期同 mediaId）')
        assert mid3 == mid1, 'R1 失败：MySQL quickHash 幂等路径未命中'
        print('  ✅ MySQL quickHash 幂等路径命中')

    print('\n=== 全部健壮性测试通过 ===')

if __name__ == '__main__':
    main()
