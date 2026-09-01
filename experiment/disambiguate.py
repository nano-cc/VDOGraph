#!/usr/bin/env python3
"""
实体消歧 - 三层级联
Level 1: 精确匹配
Level 2: 模糊匹配（字符串相似度）
Level 3: LLM 判断（Embedding 检索 + LLM 判断）
"""

import json
import re
import requests
import math
from typing import List, Dict, Optional
from difflib import SequenceMatcher

def normalize_exact(name: str) -> str:
    """规范化：小写、去空格、去标点"""
    normalized = re.sub(r'\s+', ' ', name.lower())
    return normalized.strip()

def normalize_fuzzy(name: str) -> str:
    """模糊规范化：只保留字母数字和中文"""
    normalized = re.sub(r'[^a-z0-9一-鿿]+', '', name.lower())
    return normalized

def calculate_similarity(s1: str, s2: str) -> float:
    """计算字符串相似度（0-1）"""
    return SequenceMatcher(None, s1, s2).ratio()

def has_high_entropy(name: str) -> bool:
    """判断是否有高熵（长度 >= 6 或词数 >= 2）"""
    if len(name) < 6 and len(name.split()) < 2:
        return False
    return True

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """计算余弦相似度"""
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)

class EntityDisambiguator:
    def __init__(self, api_key: str, base_url: str, model: str, embedding_model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.embedding_model = embedding_model

        # Embedding 缓存
        self.embedding_cache = {}

        # 统计
        self.stats = {
            'total': 0,
            'level1_hits': 0,
            'level2_hits': 0,
            'level3_hits': 0,
            'new_entities': 0
        }

    def disambiguate(self, all_segments: List[Dict]) -> Dict:
        """实体消歧主函数"""
        # 收集所有实体
        all_entities = []
        for segment in all_segments:
            for entity in segment['entities']:
                entity['segment_id'] = segment['segment_id']
                entity['segment_index'] = segment['segment_index']
                all_entities.append(entity)

        self.stats['total'] = len(all_entities)

        # 标准实体列表（消歧后的实体）
        canonical_entities = []
        merge_history = []

        for entity in all_entities:
            # Level 1: 精确匹配
            match = self._level1_exact_match(entity, canonical_entities)

            if match:
                self._merge_entity(entity, match, 'level1_exact')
                self.stats['level1_hits'] += 1
                merge_history.append({
                    'entity': entity['name'],
                    'merged_to': match['name'],
                    'method': 'level1_exact',
                    'confidence': 1.0
                })
                continue

            # Level 2: 模糊匹配
            match = self._level2_fuzzy_match(entity, canonical_entities)

            if match:
                self._merge_entity(entity, match, 'level2_fuzzy')
                self.stats['level2_hits'] += 1
                merge_history.append({
                    'entity': entity['name'],
                    'merged_to': match['name'],
                    'method': 'level2_fuzzy',
                    'confidence': 0.9
                })
                continue

            # Level 3: LLM 判断（Embedding 检索 + LLM 判断）
            match = self._level3_llm_judge(entity, canonical_entities)

            if match:
                self._merge_entity(entity, match, 'level3_llm')
                self.stats['level3_hits'] += 1
                merge_history.append({
                    'entity': entity['name'],
                    'merged_to': match['name'],
                    'method': 'level3_llm',
                    'confidence': 0.95
                })
                continue

            # 未匹配，创建新实体
            canonical_entity = self._create_canonical_entity(entity)
            canonical_entities.append(canonical_entity)
            self.stats['new_entities'] += 1

        # 更新统计
        self.stats['canonical_count'] = len(canonical_entities)
        self.stats['merge_rate'] = (self.stats['level1_hits'] + self.stats['level2_hits'] + self.stats['level3_hits']) / self.stats['total'] if self.stats['total'] > 0 else 0

        return {
            'canonical_entities': canonical_entities,
            'merge_history': merge_history,
            'statistics': self.stats
        }

    def _level1_exact_match(self, entity: Dict, canonical_entities: List[Dict]) -> Optional[Dict]:
        """Level 1: 精确匹配"""
        normalized = normalize_exact(entity['name'])

        for canonical in canonical_entities:
            if normalize_exact(canonical['name']) == normalized:
                return canonical

            # 也检查别名
            for alias in canonical.get('aliases', []):
                if normalize_exact(alias) == normalized:
                    return canonical

        return None

    def _level2_fuzzy_match(self, entity: Dict, canonical_entities: List[Dict]) -> Optional[Dict]:
        """Level 2: 模糊匹配"""
        normalized = normalize_fuzzy(entity['name'])

        # 熵门控：低熵跳过
        if not has_high_entropy(normalized):
            return None

        best_match = None
        best_score = 0.0
        threshold = 0.85  # 相似度阈值

        for canonical in canonical_entities:
            # 检查标准名称
            score = calculate_similarity(normalized, normalize_fuzzy(canonical['name']))
            if score > best_score:
                best_score = score
                best_match = canonical

            # 检查别名
            for alias in canonical.get('aliases', []):
                score = calculate_similarity(normalized, normalize_fuzzy(alias))
                if score > best_score:
                    best_score = score
                    best_match = canonical

        if best_score >= threshold:
            return best_match

        return None

    def _level3_llm_judge(self, entity: Dict, canonical_entities: List[Dict]) -> Optional[Dict]:
        """Level 3: LLM 判断（Embedding 检索 + LLM 判断）"""
        if not canonical_entities:
            return None

        print(f"\n  [Level 3] 处理实体: {entity['name']} ({entity['type']})")

        # 1. 用 Embedding 检索候选
        entity_embedding = self._get_embedding(entity['name'])

        if not entity_embedding:
            print(f"    ❌ Embedding 获取失败")
            return None

        # 计算所有标准实体的相似度
        candidates_with_score = []
        for canonical in canonical_entities:
            canonical_embedding = self._get_embedding(canonical['name'])
            if canonical_embedding:
                score = cosine_similarity(entity_embedding, canonical_embedding)
                candidates_with_score.append((canonical, score))

        # 按相似度排序，取 Top-5
        candidates_with_score.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates_with_score[:5]

        print(f"    Top-5 候选:")
        for idx, (c, s) in enumerate(top_candidates):
            print(f"      {idx}. {c['name']} ({c['type']}) - 相似度: {s:.4f}")

        # 过滤掉相似度太低的（阈值 0.5）
        top_candidates = [(c, s) for c, s in top_candidates if s > 0.5]

        if not top_candidates:
            print(f"    ❌ 没有候选超过阈值 0.5")
            return None

        print(f"    过滤后候选数: {len(top_candidates)}")

        # 2. LLM 判断
        candidates = [c for c, s in top_candidates]

        prompt = f"""You are an entity deduplication assistant.

NEW ENTITY:
{entity['name']} ({entity['type']})
Description: {entity['description']}

EXISTING ENTITIES:
"""
        for idx, candidate in enumerate(candidates):
            prompt += f"{idx}. {candidate['name']} ({candidate['type']})\n   Description: {candidate['description']}\n\n"

        prompt += """Entities should only be considered duplicates if they refer to the *same real-world object or concept*.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They have similar names or purposes but refer to separate instances or concepts.

Task:
Determine if the NEW ENTITY is a duplicate of any EXISTING ENTITY.
Return the index of the matching entity (0-4), or -1 if no duplicate exists.

Return ONLY a JSON object:
{
  "duplicate_index": 0 or 1 or 2 or 3 or 4 or -1,
  "confidence": 0.95,
  "reason": "brief explanation"
}
"""

        try:
            response = self._call_llm(prompt)
            print(f"    LLM 返回: {response[:200]}...")

            # 提取 JSON（可能包含在 ```json ... ``` 中）
            if '```json' in response:
                response = response.split('```json')[1].split('```')[0].strip()
            elif '```' in response:
                response = response.split('```')[1].split('```')[0].strip()

            result = json.loads(response)

            duplicate_index = result.get('duplicate_index', -1)
            reason = result.get('reason', '')

            print(f"    LLM 判断: duplicate_index={duplicate_index}, reason={reason}")

            if 0 <= duplicate_index < len(candidates):
                print(f"    ✅ 合并到: {candidates[duplicate_index]['name']}")
                return candidates[duplicate_index]
            else:
                print(f"    ❌ 未合并（LLM 判断不是同一实体）")

        except Exception as e:
            print(f"  ⚠️  LLM 判断失败: {e}")

        return None

    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """获取文本的 Embedding（带缓存）"""
        if text in self.embedding_cache:
            return self.embedding_cache[text]

        try:
            url = f"{self.base_url}/embeddings"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": self.embedding_model,
                "input": text
            }

            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()

            result = response.json()
            embedding = result['data'][0]['embedding']

            # 缓存
            self.embedding_cache[text] = embedding

            return embedding

        except Exception as e:
            print(f"  ⚠️  Embedding 获取失败: {e}")
            return None

    def _call_llm(self, prompt: str) -> str:
        """调用 LLM"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "max_tokens": 500
        }

        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()

        result = response.json()
        return result['choices'][0]['message']['content']

    def _create_canonical_entity(self, entity: Dict) -> Dict:
        """创建标准实体"""
        return {
            'id': f"entity_{normalize_fuzzy(entity['name'])}",
            'name': entity['name'],
            'type': entity['type'],
            'description': entity['description'],
            'aliases': [],
            'sources': [{
                'segment_id': entity['segment_id'],
                'segment_index': entity['segment_index']
            }],
            'source_count': 1
        }

    def _merge_entity(self, entity: Dict, canonical: Dict, method: str):
        """合并实体"""
        # 添加别名
        if entity['name'] != canonical['name'] and entity['name'] not in canonical['aliases']:
            canonical['aliases'].append(entity['name'])

        # 添加来源
        canonical['sources'].append({
            'segment_id': entity['segment_id'],
            'segment_index': entity['segment_index']
        })
        canonical['source_count'] += 1

def main():
    # 加载抽取结果
    with open('/mnt/Data/projs/Java/DOVideo-AI/experiment/extraction_results.json', 'r', encoding='utf-8') as f:
        all_segments = json.load(f)

    print("=== 实体消歧 ===")
    print(f"总片段数: {len(all_segments)}")
    print(f"总实体数: {sum(len(s['entities']) for s in all_segments)}")
    print()

    # 加载配置
    env = {}
    with open('/mnt/Data/projs/Java/DOVideo-AI/.env', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key] = value

    # 创建消歧器
    disambiguator = EntityDisambiguator(
        api_key=env.get('SILICONFLOW_API_KEY'),
        base_url=env.get('SILICONFLOW_BASE_URL', 'https://api.siliconflow.cn/v1'),
        model=env.get('LLM_MODEL', 'deepseek-ai/DeepSeek-V3.2'),
        embedding_model=env.get('EMBEDDING_MODEL', 'BAAI/bge-m3')
    )

    # 执行消歧
    result = disambiguator.disambiguate(all_segments)

    # 保存结果
    output_file = '/mnt/Data/projs/Java/DOVideo-AI/experiment/disambiguation_results.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 消歧完成")
    print(f"标准实体数: {result['statistics']['canonical_count']}")
    print(f"合并率: {result['statistics']['merge_rate']:.1%}")
    print(f"Level 1 命中: {result['statistics']['level1_hits']}")
    print(f"Level 2 命中: {result['statistics']['level2_hits']}")
    print(f"Level 3 命中: {result['statistics']['level3_hits']}")
    print(f"新实体: {result['statistics']['new_entities']}")
    print(f"\n结果已保存: {output_file}")

if __name__ == "__main__":
    main()
