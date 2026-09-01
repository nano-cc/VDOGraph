#!/usr/bin/env python3
"""
测试实体消歧和关系冲突检测
"""
import requests
import json

BASE_URL = "http://localhost:8000/api/v1"

def test_disambiguation():
    """测试实体消歧"""
    url = f"{BASE_URL}/disambiguation/disambiguate"

    # 读取测试数据
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r') as f:
        segments = json.load(f)

    # 只测试前 3 个片段
    test_segments = segments[:3]

    response = requests.post(url, json={"segments": test_segments})
    print(f"Disambiguation: {response.status_code}")

    if response.status_code == 200:
        result = response.json()
        print(f"标准实体数: {result['statistics']['canonical_count']}")
        print(f"合并率: {result['statistics']['merge_rate']:.1%}")
        print(f"Level 1 命中: {result['statistics']['level1_hits']}")
        print(f"Level 2 命中: {result['statistics']['level2_hits']}")
        print(f"Level 3 命中: {result['statistics']['level3_hits']}")
        return result
    else:
        print(f"Error: {response.text}")
        return None

def test_conflict_detection(canonical_entities):
    """测试关系冲突检测"""
    url = f"{BASE_URL}/conflict/detect"

    # 读取测试数据
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r') as f:
        segments = json.load(f)

    # 只测试前 3 个片段
    test_segments = segments[:3]

    request_data = {
        "segments": test_segments,
        "canonical_entities": canonical_entities
    }

    response = requests.post(url, json=request_data)
    print(f"\nConflict Detection: {response.status_code}")

    if response.status_code == 200:
        result = response.json()
        print(f"标准关系数: {result['statistics']['active']}")
        print(f"重复关系数: {result['statistics']['duplicates']}")
        return result
    else:
        print(f"Error: {response.text}")
        return None

if __name__ == "__main__":
    print("=== 测试实体消歧和关系冲突检测 ===")
    print()

    print("1. 实体消歧")
    disambiguation_result = test_disambiguation()

    if disambiguation_result:
        print("\n2. 关系冲突检测")
        test_conflict_detection(disambiguation_result['canonical_entities'])
