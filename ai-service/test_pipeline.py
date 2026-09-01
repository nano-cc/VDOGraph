#!/usr/bin/env python3
"""
测试完整视频处理 Pipeline
"""
import requests
import json

BASE_URL = "http://localhost:8000/api/v1"

def test_full_pipeline():
    """测试完整 Pipeline"""
    url = f"{BASE_URL}/pipeline/process"

    request_data = {
        "video_path": "/home/cong/视频/中国人牛肉自由.mp4",
        "media_id": 1,
        "user_goal": "分析牛肉价格"
    }

    print("=== 测试完整视频处理 Pipeline ===")
    print(f"请求: {json.dumps(request_data, indent=2, ensure_ascii=False)}")
    print()

    try:
        response = requests.post(url, json=request_data, timeout=7200)  # 2 小时超时
        print(f"状态码: {response.status_code}")

        if response.status_code == 200:
            result = response.json()
            print(f"✅ Pipeline 处理成功")
            print(f"统计信息: {json.dumps(result['statistics'], indent=2, ensure_ascii=False)}")
            print(f"耗时: {result['duration_ms']:.2f}ms")
        else:
            print(f"❌ Pipeline 处理失败")
            print(f"错误: {response.text}")

    except Exception as e:
        print(f"❌ 请求失败: {e}")

if __name__ == "__main__":
    test_full_pipeline()
