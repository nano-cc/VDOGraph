#!/usr/bin/env python3
"""模型设置界面 UI 验证"""
import json as _json
import requests
from playwright.sync_api import sync_playwright

login = requests.post('http://localhost:9090/user/login', json={'username': 'kgtest', 'password': 'test123456'})
data = login.json()['data']
token = data['token']
user_json = _json.dumps(data['userInfo'], ensure_ascii=False)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/google-chrome')
    page = browser.new_page(viewport={'width': 1600, 'height': 1000})
    page.goto('http://localhost:5173/')
    page.evaluate(f"localStorage.setItem('authToken', '{token}')")
    page.evaluate(f"localStorage.setItem('user', {user_json!r})")
    page.reload()
    page.wait_for_timeout(3000)

    # 打开设置
    page.click('.settings-btn')
    page.wait_for_selector('.settings-modal', timeout=10000)
    page.wait_for_timeout(1500)
    page.screenshot(path='/tmp/settings_1.png')

    # 测试 LLM 连接
    page.locator('.settings-section').first.locator('.settings-test-btn').click()
    page.wait_for_selector('.settings-test-result', timeout=20000)
    result_text = page.locator('.settings-test-result').first.inner_text()
    print(f"LLM 测试连接结果: {result_text}")
    page.screenshot(path='/tmp/settings_2.png')

    browser.close()
print("UI 验证完成")
