#!/usr/bin/env python3
"""
S3 直传上传链路 API 级测试（模拟浏览器流程，只测上传不测解析）
T1 小文件正常上传 / T2 大文件多分片 / T3 秒传 / T4 断点续传
T6 损坏视频 ffprobe / T7 主动 abort
"""
import requests
import xxhash
import os

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024

def login():
    r = requests.post(f'{BASE}/user/login', json={'username': 'kgtest', 'password': 'test123456'})
    return {'Authorization': 'Bearer ' + r.json()['data']['token']}

def quick_hash(path):
    h = xxhash.xxh3_128()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def init_upload(headers, filename, size, total, qh):
    r = requests.post(f'{BASE}/media/v2/init-upload',
                      params={'filename': filename, 'size': size, 'totalChunks': total, 'quickHash': qh},
                      headers=headers)
    r.raise_for_status()
    return r.json()['data']

def presign(headers, upload_id, part_numbers):
    r = requests.post(f'{BASE}/media/v2/presign',
                      params={'uploadId': upload_id, 'partNumbers': part_numbers},
                      headers=headers)
    r.raise_for_status()
    return r.json()['data']

def put_chunk(url, data):
    r = requests.put(url, data=data)
    r.raise_for_status()

def complete(headers, upload_id):
    r = requests.post(f'{BASE}/media/v2/complete-upload', params={'uploadId': upload_id}, headers=headers)
    return r

def upload_file(headers, path, skip_parts=()):
    """完整直传流程，返回 (media_id, file 大小)"""
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    qh = quick_hash(path)
    init = init_upload(headers, os.path.basename(path), size, total, qh)
    if init['status'] == 'INSTANT':
        return init['mediaId'], 'INSTANT'
    upload_id = init['uploadId']
    uploaded = {p['partNumber'] for p in init.get('uploadedParts', [])}
    with open(path, 'rb') as f:
        for i in range(1, total + 1):
            if i in uploaded or i in skip_parts:
                continue
            f.seek((i - 1) * CHUNK)
            data = f.read(CHUNK)
            url = presign(headers, upload_id, [i])[str(i)]
            put_chunk(url, data)
    resp = complete(headers, upload_id)
    return resp, 'DONE'

def main():
    headers = login()

    # T1: 小文件正常上传
    print('=== T1 小文件正常上传 ===')
    resp, status = upload_file(headers, '/tmp/boston_1seg.mp4')
    if status == 'INSTANT':
        print('已存在（上次测试已传），命中秒传 INSTANT, mediaId =', resp)
    else:
        print('complete:', resp.status_code, resp.json() if resp.status_code != 200 else resp.json()['data'])

    # T2: 大文件多分片（牛肉 72MB，15 片）
    print('\n=== T2 大文件多分片上传 ===')
    resp, status = upload_file(headers, '/tmp/beef_1seg.mp4')
    if status == 'INSTANT':
        print('已存在，命中秒传 INSTANT, mediaId =', resp)
    else:
        print('complete:', resp.status_code, resp.json().get('data') if resp.status_code == 200 else resp.text[:200])

    # T3: 同文件重复上传 → 秒传
    print('\n=== T3 秒传 ===')
    path = '/tmp/boston_1seg.mp4'
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    qh = quick_hash(path)
    init = init_upload(headers, os.path.basename(path), size, total, qh)
    print('status:', init['status'], '(预期 INSTANT)')
    assert init['status'] == 'INSTANT', 'T3 秒传失败'

    # T4: 断点续传（传 1 片后中断，再 init 应 RESUME，补片后完成）
    print('\n=== T4 断点续传 ===')
    path = '/tmp/boston_2seg.mp4'
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    qh = quick_hash(path)
    # 先新建并只传第 1 片
    init = init_upload(headers, os.path.basename(path), size, total, qh)
    if init['status'] == 'NEW':
        upload_id = init['uploadId']
        with open(path, 'rb') as f:
            data = f.read(CHUNK)
        put_chunk(presign(headers, upload_id, [1])['1'], data)
        print('已传第 1 片，模拟中断')
        # 重新 init，应 RESUME
        init2 = init_upload(headers, os.path.basename(path), size, total, qh)
        print('再次 init status:', init2['status'], '已传:', init2.get('uploadedParts'))
        assert init2['status'] == 'RESUME', 'T4 续传找回失败'
        assert init2['uploadedParts'][0]['partNumber'] == 1, 'T4 已传片号不对'
        upload_id = init2['uploadId']
        with open(path, 'rb') as f:
            for i in range(2, total + 1):
                f.seek((i - 1) * CHUNK)
                put_chunk(presign(headers, upload_id, [i])[str(i)], f.read(CHUNK))
        resp = complete(headers, upload_id)
        print('续传完成:', resp.status_code, resp.json().get('data') if resp.status_code == 200 else resp.text[:200])
    else:
        print('status:', init['status'], '（文件已存在走 INSTANT，T4 跳过）')

    # T6: 损坏视频（截断的 mp4）
    print('\n=== T6 损坏视频 ffprobe ===')
    with open('/tmp/boston_1seg.mp4', 'rb') as f:
        head = f.read(100 * 1024)
    with open('/tmp/broken.mp4', 'wb') as f:
        f.write(head)
    resp, _ = upload_file(headers, '/tmp/broken.mp4')
    print('complete:', resp.status_code, (resp.json().get('message') or '')[:120])
    assert resp.status_code != 200, 'T6 损坏文件应该被拒绝'

    # T7: 主动 abort
    print('\n=== T7 主动 abort ===')
    path = '/tmp/boston_3seg.mp4'
    size = os.path.getsize(path)
    total = (size + CHUNK - 1) // CHUNK
    qh = quick_hash(path)
    init = init_upload(headers, 't7_' + os.path.basename(path), size, total, qh)
    if init['status'] == 'NEW':
        upload_id = init['uploadId']
        r = requests.post(f'{BASE}/media/v2/abort', params={'uploadId': upload_id}, headers=headers)
        print('abort:', r.status_code)
        # abort 后 complete 应失败
        resp = complete(headers, upload_id)
        print('abort 后 complete:', resp.status_code, '(应失败)')
    else:
        print('status:', init['status'], '（命中已有，T7 跳过）')

    print('\n=== 全部完成 ===')

if __name__ == '__main__':
    main()
