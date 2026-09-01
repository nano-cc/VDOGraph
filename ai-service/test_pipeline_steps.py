#!/usr/bin/env python3
"""
分步骤测试完整链路
使用之前保存的中间结果，不需要重新跑视频解析和实体关系抽取
"""
import requests
import json
import time

BASE_URL = "http://localhost:8000/api/v1"

def test_disambiguation():
    """测试实体消歧（使用之前保存的结果）"""
    print("=== 测试实体消歧 ===")

    # 加载之前保存的实体关系抽取结果
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r') as f:
        segments = json.load(f)

    # 只测试前 3 个片段
    test_segments = segments[:3]

    print(f"测试片段数: {len(test_segments)}")
    print(f"总实体数: {sum(len(s['entities']) for s in test_segments)}")
    print(f"总关系数: {sum(len(s['relationships']) for s in test_segments)}")
    print()

    url = f"{BASE_URL}/disambiguation/disambiguate"

    start_time = time.time()
    response = requests.post(url, json={"segments": test_segments}, timeout=3600)
    duration = (time.time() - start_time) * 1000

    print(f"状态码: {response.status_code}")
    print(f"耗时: {duration:.2f}ms")

    if response.status_code == 200:
        result = response.json()
        print(f"✅ 实体消歧成功")
        print(f"标准实体数: {result['statistics']['canonical_count']}")
        print(f"合并率: {result['statistics']['merge_rate']:.1%}")
        print(f"Level 1 命中: {result['statistics']['level1_hits']}")
        print(f"Level 2 命中: {result['statistics']['level2_hits']}")
        print(f"Level 3 命中: {result['statistics']['level3_hits']}")
        return result
    else:
        print(f"❌ 实体消歧失败: {response.text}")
        return None

def test_conflict_detection(disambiguation_result):
    """测试关系冲突检测（使用之前保存的结果）"""
    print("\n=== 测试关系冲突检测 ===")

    # 加载之前保存的实体关系抽取结果
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r') as f:
        segments = json.load(f)

    # 只测试前 3 个片段
    test_segments = segments[:3]

    url = f"{BASE_URL}/conflict/detect"

    start_time = time.time()
    response = requests.post(url, json={
        "segments": test_segments,
        "canonical_entities": disambiguation_result['canonical_entities']
    }, timeout=600)
    duration = (time.time() - start_time) * 1000

    print(f"状态码: {response.status_code}")
    print(f"耗时: {duration:.2f}ms")

    if response.status_code == 200:
        result = response.json()
        print(f"✅ 关系冲突检测成功")
        print(f"标准关系数: {result['statistics']['active']}")
        print(f"重复关系数: {result['statistics']['duplicates']}")
        return result
    else:
        print(f"❌ 关系冲突检测失败: {response.text}")
        return None

def test_community_detection(disambiguation_result, conflict_result):
    """测试社区检测（使用之前保存的结果）"""
    print("\n=== 测试社区检测 ===")

    url = f"{BASE_URL}/community/detect"

    start_time = time.time()
    response = requests.post(url, json={
        "entities": disambiguation_result['canonical_entities'],
        "relationships": conflict_result['canonical_relationships']
    }, timeout=600)
    duration = (time.time() - start_time) * 1000

    print(f"状态码: {response.status_code}")
    print(f"耗时: {duration:.2f}ms")

    if response.status_code == 200:
        result = response.json()
        print(f"✅ 社区检测成功")
        print(f"社区数: {result['statistics']['total_communities']}")
        print(f"平均社区大小: {result['statistics']['avg_community_size']:.1f}")
        return result
    else:
        print(f"❌ 社区检测失败: {response.text}")
        return None

if __name__ == "__main__":
    print("=== 分步骤测试完整链路 ===")
    print("使用之前保存的中间结果，不需要重新跑视频解析和实体关系抽取")
    print()

    # 1. 实体消歧
    disambiguation_result = test_disambiguation()

    if disambiguation_result:
        # 2. 关系冲突检测
        conflict_result = test_conflict_detection(disambiguation_result)

        if conflict_result:
            # 3. 社区检测
            community_result = test_community_detection(disambiguation_result, conflict_result)

            if community_result:
                print("\n=== 所有测试通过 ===")
