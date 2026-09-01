#!/usr/bin/env python3
"""
测试视频解析完整链路
"""
import requests
import json
import sys

BASE_URL = "http://localhost:8000/api/v1"

def test_video_parse():
    """测试视频解析"""
    url = f"{BASE_URL}/video/parse"

    # 使用已有的视频（从 Redis 中获取）
    # 这里需要先获取视频路径
    # 暂时使用一个测试视频
    request_data = {
        "video_path": "/home/cong/视频/中国人牛肉自由.mp4",
        "user_goal": "分析牛肉价格"
    }

    print("=== 测试视频解析 ===")
    print(f"请求: {json.dumps(request_data, indent=2, ensure_ascii=False)}")
    print()

    try:
        response = requests.post(url, json=request_data, timeout=600)
        print(f"状态码: {response.status_code}")

        if response.status_code == 200:
            result = response.json()
            print(f"✅ 视频解析成功")
            print(f"片段数: {len(result['context']['segments'])}")
            print()
            print("前 3 个片段:")
            for i, segment in enumerate(result['context']['segments'][:3]):
                print(f"\n片段 {i}:")
                print(f"  时间: {segment['start_ms']//1000}s - {segment['end_ms']//1000}s")
                print(f"  ASR 文本长度: {len(segment['transcript'])}")
                print(f"  OCR 文本数量: {len(segment['ocr_texts'])}")
                print(f"  关键帧数量: {len(segment['evidence_frames'])}")
                print(f"  ASR 文本: {segment['transcript'][:100]}...")
            return result['context']
        else:
            print(f"❌ 视频解析失败")
            print(f"错误: {response.text}")
            return None

    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return None

def test_full_pipeline(video_context):
    """测试完整链路"""
    if not video_context:
        print("❌ 视频解析失败，无法测试完整链路")
        return

    print("\n=== 测试完整链路 ===")

    # 1. 实体关系抽取
    print("\n1. 实体关系抽取")
    url = f"{BASE_URL}/entity/extract"

    segments = []
    for i, segment in enumerate(video_context['segments']):
        segments.append({
            "segment_id": f"media_1_segment_{i}",
            "segment_index": i,
            "start_ms": segment['start_ms'],
            "end_ms": segment['end_ms'],
            "transcript": segment['transcript'],
            "ocr_texts": segment['ocr_texts'],
            "entities": [],
            "relationships": []
        })

    response = requests.post(url, json={"segments": segments}, timeout=600)
    if response.status_code == 200:
        result = response.json()
        print(f"✅ 实体关系抽取成功")
        print(f"总实体数: {sum(len(s['entities']) for s in result['results'])}")
        print(f"总关系数: {sum(len(s['relationships']) for s in result['results'])}")
        extract_result = result['results']
    else:
        print(f"❌ 实体关系抽取失败: {response.text}")
        return

    # 2. 实体消歧
    print("\n2. 实体消歧")
    url = f"{BASE_URL}/disambiguation/disambiguate"

    # 转换格式：驼峰 → 下划线
    disambiguation_segments = []
    for segment in extract_result:
        disambiguation_segments.append({
            "segment_id": segment['segmentId'],
            "segment_index": segment['segmentIndex'],
            "start_ms": segment['startMs'],
            "end_ms": segment['endMs'],
            "transcript": segment['transcript'],
            "ocr_texts": segment['ocrTexts'],
            "entities": segment['entities'],
            "relationships": segment['relationships']
        })

    response = requests.post(url, json={"segments": disambiguation_segments}, timeout=600)
    if response.status_code == 200:
        result = response.json()
        print(f"✅ 实体消歧成功")
        print(f"标准实体数: {result['statistics']['canonical_count']}")
        print(f"合并率: {result['statistics']['merge_rate']:.1%}")
        disambiguation_result = result
    else:
        print(f"❌ 实体消歧失败: {response.text}")
        return

    # 3. 关系冲突检测
    print("\n3. 关系冲突检测")
    url = f"{BASE_URL}/conflict/detect"

    response = requests.post(url, json={
        "segments": disambiguation_segments,
        "canonical_entities": disambiguation_result['canonical_entities']
    }, timeout=600)
    if response.status_code == 200:
        result = response.json()
        print(f"✅ 关系冲突检测成功")
        print(f"标准关系数: {result['statistics']['active']}")
        print(f"重复关系数: {result['statistics']['duplicates']}")
        conflict_result = result
    else:
        print(f"❌ 关系冲突检测失败: {response.text}")
        return

    # 4. 社区检测
    print("\n4. 社区检测")
    url = f"{BASE_URL}/community/detect"

    response = requests.post(url, json={
        "entities": disambiguation_result['canonical_entities'],
        "relationships": conflict_result['canonical_relationships']
    }, timeout=600)
    if response.status_code == 200:
        result = response.json()
        print(f"✅ 社区检测成功")
        print(f"社区数: {result['statistics']['total_communities']}")
        print(f"平均社区大小: {result['statistics']['avg_community_size']:.1f}")
    else:
        print(f"❌ 社区检测失败: {response.text}")
        return

    print("\n=== 完整链路测试通过 ===")

if __name__ == "__main__":
    # 检查是否有测试视频
    import os
    video_path = "/home/cong/视频/中国人牛肉自由.mp4"
    if not os.path.exists(video_path):
        print(f"❌ 测试视频不存在: {video_path}")
        sys.exit(1)

    print(f"✅ 找到测试视频: {video_path}")
    print(f"文件大小: {os.path.getsize(video_path) / 1024 / 1024:.1f} MB")
    print()

    # 测试视频解析
    video_context = test_video_parse()

    # 测试完整链路
    test_full_pipeline(video_context)
