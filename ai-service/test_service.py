#!/usr/bin/env python3
"""
测试 Python 服务
"""
import requests
import json

BASE_URL = "http://localhost:8000/api/v1"

def test_health():
    """测试健康检查"""
    response = requests.get("http://localhost:8000/health")
    print(f"Health check: {response.status_code}")
    print(response.json())

def test_extract_entities():
    """测试实体关系抽取"""
    url = f"{BASE_URL}/entity/extract"

    # 读取测试数据
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r') as f:
        segments = json.load(f)

    # 只测试第一个片段
    test_segments = segments[:1]

    response = requests.post(url, json={"segments": test_segments})
    print(f"Extract entities: {response.status_code}")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))

def test_detect_communities():
    """测试社区检测"""
    url = f"{BASE_URL}/community/detect"

    # 读取测试数据
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json', 'r') as f:
        disambiguation_data = json.load(f)

    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/conflict_detection_results.json', 'r') as f:
        conflict_data = json.load(f)

    request_data = {
        "entities": disambiguation_data['canonical_entities'],
        "relationships": conflict_data['canonical_relationships']
    }

    response = requests.post(url, json=request_data)
    print(f"Detect communities: {response.status_code}")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    print("=== 测试 Python 服务 ===")
    print()

    print("1. 健康检查")
    test_health()
    print()

    print("2. 实体关系抽取")
    test_extract_entities()
    print()

    print("3. 社区检测")
    test_detect_communities()
    print()
