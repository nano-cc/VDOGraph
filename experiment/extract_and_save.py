#!/usr/bin/env python3
"""
实体关系抽取 - 保存为 JSON
"""

import redis
import json
import requests
from typing import List, Dict

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
    seconds = ms // 1000
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes:02d}:{secs:02d}"

def build_input_text(segment: Dict) -> str:
    text = f"[时间: {format_time(segment['startMs'])}-{format_time(segment['endMs'])}]\n"
    text += "[语音内容]\n"
    text += segment['transcript'] + "\n"

    if segment.get('ocrTexts'):
        text += "\n[画面文字]\n"
        for ocr_text in segment['ocrTexts']:
            if 'Error' not in ocr_text and 'Estimating' not in ocr_text:
                text += ocr_text + "\n"

    return text

def build_extraction_prompt(input_text: str) -> str:
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

def parse_llm_output(output: str) -> tuple:
    """解析 LLM 输出，返回 (entities, relationships)"""
    entities = []
    relationships = []

    lines = output.split('\n')
    for line in lines:
        line = line.strip()
        if not line or line == '##' or line == '<|COMPLETE|>':
            continue

        if line.startswith('("entity"'):
            parts = line.split('<|>')
            if len(parts) >= 4:
                entity = {
                    'name': parts[1].strip(),
                    'type': parts[2].strip(),
                    'description': parts[3].strip().rstrip(')')
                }
                entities.append(entity)

        elif line.startswith('("relationship"'):
            parts = line.split('<|>')
            if len(parts) >= 5:
                relationship = {
                    'source': parts[1].strip(),
                    'target': parts[2].strip(),
                    'description': parts[3].strip(),
                    'strength': parts[4].strip().rstrip(')')
                }
                relationships.append(relationship)

    return entities, relationships

def main():
    env = load_env()
    redis_password = env.get('REDIS_PASSWORD')
    api_key = env.get('SILICONFLOW_API_KEY')
    base_url = env.get('SILICONFLOW_BASE_URL', 'https://api.siliconflow.cn/v1')
    model = env.get('LLM_MODEL', 'deepseek-ai/DeepSeek-V3.2')

    r = redis.Redis(host='localhost', port=6379, password=redis_password, decode_responses=True)

    media_id = 1
    context_key = f"agent:checkpoint:{media_id}"
    context_data = r.hget(context_key, "context")

    if not context_data:
        print(f"❌ VideoContext not found for mediaId={media_id}")
        sys.exit(1)

    context = json.loads(context_data)
    segments = context['segments']

    # 测试所有片段
    test_count = len(segments)

    print(f"=== 开始处理 {test_count} 个片段 ===")
    print()

    all_results = []

    for i in range(test_count):
        segment = segments[i]

        print(f"处理 Segment {i}...")

        input_text = build_input_text(segment)
        prompt = build_extraction_prompt(input_text)
        response = call_deepseek_api(prompt, api_key, base_url, model)

        entities, relationships = parse_llm_output(response)

        result = {
            'segment_id': f"media_{media_id}_segment_{i}",
            'segment_index': i,
            'start_ms': segment['startMs'],
            'end_ms': segment['endMs'],
            'transcript': segment['transcript'],
            'ocr_texts': [t for t in segment.get('ocrTexts', []) if 'Error' not in t and 'Estimating' not in t],
            'entities': entities,
            'relationships': relationships
        }

        all_results.append(result)

        print(f"  - 实体: {len(entities)} 个")
        print(f"  - 关系: {len(relationships)} 个")

    # 保存为 JSON
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 结果已保存: {output_file}")
    print(f"总片段数: {len(all_results)}")
    print(f"总实体数: {sum(len(r['entities']) for r in all_results)}")
    print(f"总关系数: {sum(len(r['relationships']) for r in all_results)}")

if __name__ == "__main__":
    main()
