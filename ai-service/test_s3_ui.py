#!/usr/bin/env python3
"""前端 S3 直传 UI 测试：真实上传 + 伪装文件拒绝"""
import json as _json
import requests
from playwright.sync_api import sync_playwright

login = requests.post('http://localhost:9090/user/login', json={'username': 'kgtest', 'password': 'test123456'})
data = login.json()['data']
token = data['token']
user_json = _json.dumps(data['userInfo'], ensure_ascii=False)

# 造伪装文件
with open('/tmp/fake.mp4', 'wb') as f:
    f.write(b'MZ' + b'\x00' * 1024 * 100)  # exe 头

# 造一个真实新视频（不同时间段）
import subprocess
subprocess.run(['ffmpeg', '-y', '-ss', '180', '-t', '30', '-i', '/home/cong/视频/波士顿圆脸.mp4',
                '-c:v', 'libx264', '-c:a', 'aac', '/tmp/boston_ui_test.mp4'], capture_output=True)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/google-chrome')
    page = browser.new_page(viewport={'width': 1600, 'height': 1000})
    page.goto('http://localhost:5173/')
    page.evaluate(f"localStorage.setItem('authToken', '{token}')")
    page.evaluate(f"localStorage.setItem('user', {user_json!r})")
    page.reload()
    page.wait_for_timeout(3000)

    # T5: 伪装文件（exe 改名 mp4）
    page.set_input_files('#file-input', '/tmp/fake.mp4')
    page.wait_for_timeout(4000)
    toast = page.locator('.notification-bar')
    msg = toast.inner_text() if toast.count() else '（无提示出现）'
    print(f"T5 伪装文件提示: {msg[:100]}")

    # 真实上传：UI 全流程（指纹→init→presign→直传→complete），上传是自动触发的
    page.set_input_files('#file-input', '/tmp/boston_ui_test.mp4')
    try:
        page.wait_for_selector('.notification-bar:has-text("上传完成"), .notification-bar:has-text("秒传")', timeout=180000)
        toast = page.locator('.notification-bar')
        print(f"UI 真实上传: {toast.inner_text()[:80]}")
    except Exception:
        toast = page.locator('.notification-bar')
        print("UI 真实上传: 等待完成提示超时")
        print("  当前消息:", toast.inner_text()[:150] if toast.count() else '无')

    page.screenshot(path='/tmp/s3_ui_test.png')
    browser.close()

# 后端验证
r = requests.get('http://localhost:9090/media/list', headers={'Authorization': f'Bearer {token}'})
items = r.json()['data']
new_one = [m for m in items if 'boston_ui_test' in m['filename']]
print("后端媒体列表包含新上传:", len(new_one) > 0, new_one[0]['id'] if new_one else '')
