#!/usr/bin/env python3
"""
实体关系抽取测试脚本
直接从 Redis 读取 VideoContext，调用 DeepSeek API 抽取实体和关系
"""

import redis
import json
import os
import sys
import requests
from typing import List, Dict

# 从 .env 读取配置
def load_env():
    env = {}
    with open('/mnt/Data/projs/Java/DOVideo-AI/.env', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key] = value
    return env

def format_time(ms: int) -> str:
    """格式化时间（毫秒 → MM:SS）"""
    seconds = ms // 1000
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes:02d}:{secs:02d}"

def build_input_text(segment: Dict) -> str:
    """构建输入文本"""
    text = f"[时间: {format_time(segment['startMs'])}-{format_time(segment['endMs'])}]\n"
    text += "[语音内容]\n"
    text += segment['transcript'] + "\n"

    if segment.get('ocrTexts'):
        text += "\n[画面文字]\n"
        for ocr_text in segment['ocrTexts']:
            # 过滤掉 OCR 错误信息
            if 'Error' not in ocr_text and 'Estimating' not in ocr_text:
                text += ocr_text + "\n"

    return text

def build_extraction_prompt(input_text: str) -> str:
    """构建抽取 Prompt（GraphRAG 风格）"""
    return f"""-Goal-
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

-Example-
Entity_types: ORGANIZATION,PERSON
Text:
The Central Institution is scheduled to meet on Monday, with Chair Martin Smith taking questions.
######################
Output:
("entity"<|>CENTRAL INSTITUTION<|>ORGANIZATION<|>The Central Institution is setting interest rates)
##
("entity"<|>MARTIN SMITH<|>PERSON<|>Martin Smith is the chair of the Central Institution)
##
("relationship"<|>MARTIN SMITH<|>CENTRAL INSTITUTION<|>Martin Smith is the Chair<|>9)
<|COMPLETE|>

-Real Data-
Entity_types: Person, Organization, Location, Product, Concept, Event, Other
Text: {input_text}
Output:
"""

def call_deepseek_api(prompt: str, api_key: str, base_url: str, model: str) -> str:
    """调用 DeepSeek API"""
    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 2000
    }

    response = requests.post(url, headers=headers, json=data, timeout=300)
    response.raise_for_status()

    result = response.json()
    return result['choices'][0]['message']['content']

def main():
    # 加载配置
    env = load_env()
    redis_password = env.get('REDIS_PASSWORD')
    api_key = env.get('SILICONFLOW_API_KEY')
    base_url = env.get('SILICONFLOW_BASE_URL', 'https://api.siliconflow.cn/v1')
    model = env.get('LLM_MODEL', 'deepseek-ai/DeepSeek-V3.2')

    # 连接 Redis
    r = redis.Redis(host='localhost', port=6379, password=redis_password, decode_responses=True)

    # 读取 VideoContext
    media_id = 1
    context_key = f"agent:checkpoint:{media_id}"

    print("=== 从 Redis 加载 VideoContext ===")
    context_data = r.hget(context_key, "context")

    if not context_data:
        print(f"❌ VideoContext not found for mediaId={media_id}")
        sys.exit(1)

    context = json.loads(context_data)
    segments = context['segments']

    print(f"✅ 加载成功，总片段数: {len(segments)}")
    print()

    # 测试前 3 个片段
    test_count = min(3, len(segments))

    for i in range(test_count):
        segment = segments[i]

        print(f"=== Segment {i} ===")
        print(f"时间: {format_time(segment['startMs'])}-{format_time(segment['endMs'])}")
        print(f"ASR 文本长度: {len(segment['transcript'])}")
        print(f"OCR 文本数量: {len(segment.get('ocrTexts', []))}")
        print()

        # 构建输入文本
        input_text = build_input_text(segment)
        print("--- 输入文本 ---")
        print(input_text[:200] + "..." if len(input_text) > 200 else input_text)
        print()

        # 构建 Prompt
        prompt = build_extraction_prompt(input_text)

        # 调用 LLM
        print("--- 调用 LLM... ---")
        response = call_deepseek_api(prompt, api_key, base_url, model)

        print("--- LLM 返回结果 ---")
        print(response)
        print()
        print("=" * 80)
        print()

if __name__ == "__main__":
    main()
