#!/usr/bin/env python3
"""
全链路联调（模拟网页操作全流程）
F1 登录 → F2 媒体列表 → F3 上传（v2 multipart）→ F4 构建状态轮询
→ F5 问答（非流式）→ F6 流式问答（SSE 思考过程）→ F7 引用播放
→ F8 模型配置读取 → F9 删除视频（图谱联动清理）
"""
import requests, xxhash, os, sys, json, subprocess, time

BASE = 'http://localhost:9090'
CHUNK = 5 * 1024 * 1024
PASS = []

def ok(name, cond, detail=''):
    status = '✅' if cond else '❌'
    print(f'{status} {name} {detail}')
    PASS.append(cond)
    if not cond:
        print('!! 中断'); sys.exit(1)

def qh(path):
    h = xxhash.xxh3_128()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

# F1 登录
r = requests.post(f'{BASE}/user/login', json={'username': 'kgtest', 'password': 'test123456'})
token = r.json()['data']['token']
H = {'Authorization': f'Bearer {token}'}
ok('F1 登录', r.json()['code'] == 0)

# F2 媒体列表
r = requests.get(f'{BASE}/media/list', headers=H)
items = r.json()['data']
ok('F2 媒体列表', r.json()['code'] == 0, f'{len(items)} 条')

# F3 上传（v2 multipart 全流程）
src = '/home/cong/视频/波士顿圆脸.mp4'
path = '/tmp/fullchain_clip.mp4'
subprocess.run(['ffmpeg', '-y', '-ss', '550', '-t', '40', '-i', src,
                '-c:v', 'libx264', '-c:a', 'aac', path], capture_output=True, check=True)
size = os.path.getsize(path)
total = (size + CHUNK - 1) // CHUNK
hash_ = qh(path)
r = requests.post(f'{BASE}/media/v2/init-upload',
                  params={'filename': 'fullchain_clip.mp4', 'size': size, 'totalChunks': total, 'quickHash': hash_}, headers=H)
d = r.json()['data']
ok('F3.1 init-upload', d['status'] == 'NEW', f'{total} 分片')
uid = d['uploadId']
with open(path, 'rb') as f:
    for i in range(1, total + 1):
        f.seek((i - 1) * CHUNK)
        r = requests.post(f'{BASE}/media/v2/presign', params={'uploadId': uid, 'partNumbers': [i]}, headers=H)
        pr = requests.put(r.json()['data'][str(i)], data=f.read(CHUNK))
        assert pr.status_code == 200, f'分片 {i} PUT 失败 {pr.status_code}'
ok('F3.2 分片直传 MinIO', True)
t0 = time.time()
r = requests.post(f'{BASE}/media/v2/complete-upload', params={'uploadId': uid, 'quickHash': hash_}, headers=H)
d = r.json()['data']
media_id = d['mediaId']
ok('F3.3 complete-upload', r.json()['code'] == 0, f'mediaId={media_id}, complete 耗时 {time.time()-t0:.1f}s（投递即返回）')

# F4 构建状态轮询（MQ → Python 后台构建）
print('  等待构建（轮询 /kg/status）...')
for i in range(120):
    r = requests.get(f'{BASE}/kg/status', params={'mediaId': media_id}, headers=H)
    st = r.json()['data']
    status = st.get('status')
    if i % 6 == 0:
        print(f'  [{i*5}s] {status} {st.get("phase") or ""} {st.get("progress") or ""} {st.get("message") or ""}')
    if status == 'SUCCESS':
        break
    if status == 'FAILED':
        ok('F4 图谱构建', False, st.get('message'))
    time.sleep(5)
ok('F4 图谱构建（MQ异步全链路）', status == 'SUCCESS', st.get('stats', '')[:80])

# F5 问答（非流式）
r = requests.post(f'{BASE}/kg/ask', params={'question': '这个视频讲了什么？', 'mode': 'auto'}, headers=H)
d = r.json()['data']
ok('F5 问答（agent）', r.json()['code'] == 0 and d['answer'], f"工具调用 {len(d.get('tool_calls', []))} 次, 引用 {len(d['citations'])} 条")

# F6 流式问答（SSE 思考过程）
r = requests.post(f'{BASE}/kg/ask/stream', params={'question': '视频里提到威慑战略了吗？'}, headers=H, stream=True)
events = []
for line in r.iter_lines():
    if line and line.startswith(b'data: '):
        try:
            events.append(json.loads(line[6:]))
        except Exception:
            pass
types = [e.get('type') for e in events]
has_tool = 'tool_start' in types
has_final = 'final' in types
final_ev = next((e for e in events if e.get('type') == 'final'), {})
ok('F6 流式问答（思考过程）', has_tool and has_final,
   f"事件 {len(events)} 个: tool_start={types.count('tool_start')}, final={'answer' in final_ev}")
cite_media = None
for c in (final_ev.get('citations') or []):
    if c.get('media_id'):
        cite_media = c['media_id']
        break

# F7 引用播放（溯源视频可播放）
if cite_media:
    r = requests.get(f'{BASE}/media/playback', params={'id': cite_media}, headers=H)
    url = r.json()['data']
    pr = requests.get(url, headers={'Range': 'bytes=0-1023'}, timeout=10)
    ok('F7 引用视频播放', r.json()['code'] == 0 and pr.status_code in (200, 206),
       f'mediaId={cite_media} 播放地址 {pr.status_code}')
else:
    ok('F7 引用视频播放', False, '无引用')

# F8 模型配置（设置弹窗）
r = requests.get(f'{BASE}/admin/config/models', headers=H)
ok('F8 模型配置读取', r.json()['code'] == 0)

# F9 删除视频（图谱联动清理）
r = requests.delete(f'{BASE}/media/delete', params={'id': media_id}, headers=H)
ok('F9.1 删除视频', r.json()['code'] == 0)
time.sleep(10)
r = requests.get(f'{BASE}/kg/status', params={'mediaId': media_id}, headers=H)
ok('F9.2 构建状态已清理', r.json()['data'].get('status') is None)

print(f"\n{'='*40}\n全链路联调 {'全部通过 ✅' if all(PASS) else '有失败 ❌'}（{sum(PASS)}/{len(PASS)}）")
