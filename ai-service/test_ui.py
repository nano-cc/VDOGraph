#!/usr/bin/env python3
"""前端问答 UI 自动化验证：思考过程流式展示 + 溯源播放"""
import json as _json
import requests
from playwright.sync_api import sync_playwright

# 用 user_id=1 的账号（视频所有者），才能播放溯源视频
login = requests.post('http://localhost:9090/user/login', json={'username': 'cong', 'password': 'cong123'})
if login.json()['code'] != 0:
    # 尝试常见密码
    for pwd in ['123456', 'cong123456', 'password']:
        login = requests.post('http://localhost:9090/user/login', json={'username': 'cong', 'password': pwd})
        if login.json()['code'] == 0:
            break
data = login.json()['data']
if not data or not data.get('token'):
    print("user 1 登录失败，用 kgtest（视频播放可能受限）")
    login = requests.post('http://localhost:9090/user/login', json={'username': 'kgtest', 'password': 'test123456'})
    data = login.json()['data']
token = data['token']
user_json = _json.dumps(data['userInfo'], ensure_ascii=False)
print(f"登录用户: {data['userInfo']['username']} (id={data['userInfo']['id']})")

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/google-chrome')
    page = browser.new_page(viewport={'width': 1600, 'height': 1000})
    page.goto('http://localhost:5173/')
    page.evaluate(f"localStorage.setItem('authToken', '{token}')")
    page.evaluate(f"localStorage.setItem('user', {user_json!r})")
    page.reload()
    page.wait_for_timeout(3000)

    # 提问（auto 模式，流式）
    page.fill('.kg-input', '进口牛肉对国内养殖户有什么影响？')
    page.select_option('.kg-mode', 'auto')
    page.click('.kg-ask-btn')

    # 等待第一个思考事件出现（流式）
    page.wait_for_selector('.kg-event', timeout=30000)
    page.wait_for_timeout(8000)
    page.locator('.kg-section').screenshot(path='/tmp/kg_stream_1_thinking.png')
    print(f"思考事件（中途）: {page.locator('.kg-event').count()} 个")

    # 等待最终答案
    page.wait_for_selector('.kg-answer', timeout=180000)
    page.wait_for_timeout(1500)
    page.locator('.kg-section').screenshot(path='/tmp/kg_stream_2_final.png')

    answer_text = page.locator('.kg-answer').inner_text()
    print(f"思考事件（最终）: {page.locator('.kg-event').count()} 个")
    print(f"引用卡片: {page.locator('.kg-cite-item').count()} 个")
    print(f"答案前 80 字: {answer_text[:80]}")

    # 点击第一个溯源卡片 -> 视频弹窗
    page.locator('.kg-cite-item').first.click()
    try:
        page.wait_for_selector('.kg-video-player', timeout=10000)
        page.wait_for_timeout(3000)
        current_time = page.evaluate("document.querySelector('.kg-video-player').currentTime")
        title = page.locator('.kg-video-header span').inner_text()
        print(f"视频弹窗: {title}, 播放位置: {current_time:.1f}s")
        page.screenshot(path='/tmp/kg_stream_3_video.png')
    except Exception as e:
        err = page.locator('.kg-error').inner_text() if page.locator('.kg-error').count() else str(e)
        print(f"视频弹窗失败: {err}")

    browser.close()
print("UI 验证完成")
