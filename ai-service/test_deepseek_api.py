#!/usr/bin/env python3
"""
测试 DeepSeek API 可达性
"""
import requests
import time
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv('/mnt/Data/projs/Java/DOVideo-AI/.env')

def test_deepseek_api():
    """测试 DeepSeek API"""
    api_key = os.getenv('SILICONFLOW_API_KEY')
    base_url = os.getenv('SILICONFLOW_BASE_URL', 'https://api.siliconflow.cn/v1')
    model = os.getenv('LLM_MODEL', 'deepseek-ai/DeepSeek-V3.2')

    print("=== 测试 DeepSeek API 可达性 ===")
    print(f"API Key: {api_key[:20]}..." if api_key else "API Key: 未设置")
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print()

    # 测试 1: 简单的文本生成
    print("测试 1: 简单的文本生成")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"}
        ],
        "temperature": 0.0,
        "max_tokens": 100
    }

    start_time = time.time()
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        duration = (time.time() - start_time) * 1000

        print(f"  状态码: {response.status_code}")
        print(f"  耗时: {duration:.2f}ms")

        if response.status_code == 200:
            result = response.json()
            print(f"  响应: {result['choices'][0]['message']['content']}")
            print(f"  ✅ 测试通过")
        else:
            print(f"  错误: {response.text}")
            print(f"  ❌ 测试失败")

    except Exception as e:
        duration = (time.time() - start_time) * 1000
        print(f"  耗时: {duration:.2f}ms")
        print(f"  错误: {e}")
        print(f"  ❌ 测试失败")

    print()

    # 测试 2: 实体关系抽取（使用实际的 Prompt）
    print("测试 2: 实体关系抽取")
    prompt = """-Goal-
Given a text document and a list of entity types, identify all entities of those types from the text and all relationships among the identified entities.

-Steps-
1. Identify all entities. For each identified entity, extract:
- entity_name: Name of the entity, capitalized
- entity_type: One of the following types: [Person, Organization, Location, Product, Concept, Event, Other]
- entity_description: Comprehensive description of the entity
Format: ("entity"<|><entity_name><|><entity_type><|><entity_description>)

2. Identify all pairs of (source_entity, target_entity) that are clearly related.
For each pair, extract:
- source_entity: name of the source entity
- target_entity: name of the target entity
- relationship_description: explanation of the relationship
- relationship_strength: numeric score 1-10
Format: ("relationship"<|><source_entity<|><target_entity><|><relationship_description><|><relationship_strength>)

3. Return output as a single list. Use **##** as the list delimiter.

4. When finished, output <|COMPLETE|>

-Real Data-
Entity_types: Person, Organization, Location, Product, Concept, Event, Other
Text: 谁夺走了中国人的牛肉，自由大家有没有发现牛肉价格最近涨得特别厉害？
Output:
"""

    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 2000
    }

    start_time = time.time()
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        duration = (time.time() - start_time) * 1000

        print(f"  状态码: {response.status_code}")
        print(f"  耗时: {duration:.2f}ms")

        if response.status_code == 200:
            result = response.json()
            print(f"  响应长度: {len(result['choices'][0]['message']['content'])} 字符")
            print(f"  响应: {result['choices'][0]['message']['content'][:200]}...")
            print(f"  ✅ 测试通过")
        else:
            print(f"  错误: {response.text}")
            print(f"  ❌ 测试失败")

    except Exception as e:
        duration = (time.time() - start_time) * 1000
        print(f"  耗时: {duration:.2f}ms")
        print(f"  错误: {e}")
        print(f"  ❌ 测试失败")

    print()

    # 测试 3: Embedding API
    print("测试 3: Embedding API")
    url = f"{base_url}/embeddings"
    data = {
        "model": os.getenv('EMBEDDING_MODEL', 'BAAI/bge-m3'),
        "input": "进口牛肉"
    }

    start_time = time.time()
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        duration = (time.time() - start_time) * 1000

        print(f"  状态码: {response.status_code}")
        print(f"  耗时: {duration:.2f}ms")

        if response.status_code == 200:
            result = response.json()
            embedding = result['data'][0]['embedding']
            print(f"  Embedding 维度: {len(embedding)}")
            print(f"  ✅ 测试通过")
        else:
            print(f"  错误: {response.text}")
            print(f"  ❌ 测试失败")

    except Exception as e:
        duration = (time.time() - start_time) * 1000
        print(f"  耗时: {duration:.2f}ms")
        print(f"  错误: {e}")
        print(f"  ❌ 测试失败")

    print()

    # 测试 4: ASR API
    print("测试 4: ASR API")
    url = f"{base_url}/audio/transcriptions"

    # 创建一个小的测试音频文件
    import tempfile
    import subprocess

    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        test_audio = f.name

    # 用 FFmpeg 创建一个 1 秒的静音音频
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono',
        '-t', '1', '-acodec', 'libmp3lame', test_audio
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with open(test_audio, 'rb') as f:
        files = {
            'file': ('test.mp3', f, 'application/octet-stream')
        }
        data = {
            'model': os.getenv('ASR_MODEL', 'TeleAI/TeleSpeechASR')
        }

        start_time = time.time()
        try:
            response = requests.post(url, headers={'Authorization': f'Bearer {api_key}'}, files=files, data=data, timeout=60)
            duration = (time.time() - start_time) * 1000

            print(f"  状态码: {response.status_code}")
            print(f"  耗时: {duration:.2f}ms")

            if response.status_code == 200:
                result = response.json()
                print(f"  响应: {result.get('text', '')}")
                print(f"  ✅ 测试通过")
            else:
                print(f"  错误: {response.text}")
                print(f"  ❌ 测试失败")

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            print(f"  耗时: {duration:.2f}ms")
            print(f"  错误: {e}")
            print(f"  ❌ 测试失败")

    # 清理测试音频
    os.unlink(test_audio)

    print()
    print("=== 测试完成 ===")

if __name__ == "__main__":
    test_deepseek_api()
