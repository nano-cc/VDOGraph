#!/usr/bin/env python3
"""
L2 答案层 + L3 行为层评测
- L2: RAGAS context_recall + 自定义裁判（faithfulness 论断核查 / answer_relevancy）
      + 自定义引用准确率（引用的片段是否支撑答案，LLM 判断）
- L3: 越界拒答率 + 工具调用统计

用法：
  python3 eval_answers.py                # 全量
  python3 eval_answers.py --limit 5      # 试跑 5 题
  python3 eval_answers.py --skip-ragas   # 只跑问答和引用准确率（快）
"""
import sys
sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')

import json
import asyncio
import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests

import os
DATASET_PATH = os.environ.get('EVAL_DATASET', '/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval_dataset.json')
EVAL_USER_ID = int(os.environ.get('EVAL_USER_ID', '2'))  # bench 评测传 4
REPORT_DIR = Path('/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval/reports')
BASELINE_PATH = Path('/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval/baseline.json')
ANSWERS_CACHE = Path(f"/mnt/Data/projs/Java/DOVideo-AI/ai-service/eval/answers_cache_{os.environ.get('EVAL_USER_ID', '2')}.json")

API_BASE = 'http://localhost:8000/api/v1'


def get_api_key() -> str:
    import os
    return os.environ.get('AI_SERVICE_API_KEY', '')


def ask(question: str) -> Dict:
    """调 agent 问答（auto 模式）"""
    resp = requests.post(
        f'{API_BASE}/query/ask',
        headers={'Content-Type': 'application/json', 'X-API-Key': get_api_key()},
        json={'question': question, 'mode': 'auto', 'user_id': EVAL_USER_ID},
        timeout=600
    )
    resp.raise_for_status()
    return resp.json()


def collect_answers(dataset: List[Dict], limit: int = None) -> List[Dict]:
    """跑全部问答（带磁盘缓存，重跑不重复花钱）"""
    cache = {}
    if ANSWERS_CACHE.exists():
        cache = json.loads(ANSWERS_CACHE.read_text())

    items = dataset[:limit] if limit else dataset
    results = []
    for i, q in enumerate(items):
        if q['id'] in cache:
            print(f"[{i+1}/{len(items)}] {q['id']} (缓存)")
            results.append(cache[q['id']])
            continue

        print(f"[{i+1}/{len(items)}] {q['id']}: {q['question'][:40]}")
        start = time.time()
        try:
            r = ask(q['question'])
            entry = {
                'id': q['id'], 'type': q['type'], 'question': q['question'],
                'answer': r['answer'], 'citations': r.get('citations', []),
                'tool_calls': r.get('tool_calls', []),
                'duration_ms': r.get('duration_ms', 0),
            }
        except Exception as e:
            entry = {'id': q['id'], 'type': q['type'], 'question': q['question'],
                     'answer': f'ERROR: {e}', 'citations': [], 'tool_calls': [], 'duration_ms': 0}
        entry['duration_ms'] = (time.time() - start) * 1000
        results.append(entry)
        cache[q['id']] = entry
        ANSWERS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))

    return results


def merge_dataset(dataset: List[Dict], answers: List[Dict]) -> List[Dict]:
    """合并数据集标注与问答结果"""
    answer_map = {a['id']: a for a in answers}
    merged = []
    for q in dataset:
        if q['id'] in answer_map:
            merged.append({**q, **answer_map[q['id']]})
    return merged


def check_refusal(entry: Dict) -> bool:
    """越界题：答案应明确表示没有相关信息"""
    answer = entry['answer']
    refusal_markers = ['没有相关信息', '没有关于', '无法回答', '未提及', '没有提到', '知识库中没有',
                       '检索不到', '没有找到', '不涉及', '无法提供']
    return any(m in answer for m in refusal_markers)


def _fetch_full_transcripts(segment_ids: List[str]) -> Dict[str, str]:
    """从 Neo4j 取片段完整 ASR 文本（citation 里的 excerpt 只有 100 字，不够裁判核对）"""
    from app.clients.neo4j_client import Neo4jClient
    client = Neo4jClient()
    with client.driver.session() as s:
        result = s.run("""
            MATCH (s:Segment) WHERE s.id IN $ids
            RETURN s.id as id, s.transcript as transcript
        """, ids=list(segment_ids))
        mapping = {r['id']: (r['transcript'] or '') for r in result}
    client.close()
    return mapping


CLAIMS_PROMPT = """把下面的答案拆解成原子论断（每句一个事实，可独立验证）。

答案: {answer}

只返回 JSON: {{"claims": ["论断1", "论断2", ...]}}（最多 10 条，忽略问候/总结性废话）"""

VERIFY_PROMPT = """你是事实核查裁判。逐条判断论断是否被"检索上下文"支撑（允许同义改写和合理归纳，但数字/人名/事实必须一致）。

检索上下文:
{contexts}

论断:
{claims}

只返回 JSON: {{"supported": [true/false, ...]}}（与论断一一对应）"""

RELEVANCY_PROMPT = """你是评测裁判。评价答案与问题的相关程度（0~1 分）。
1.0 = 完全切题且回答了问题；0.5 = 部分切题；0 = 答非所问或拒绝回答（对库内问题拒绝算 0）。

问题: {question}
答案: {answer}

只返回 JSON: {{"score": 0.85}}"""


async def custom_judges(merged: List[Dict]) -> Dict:
    """自定义 RAGAS 替代：faithfulness（论断核查）+ answer_relevancy"""
    from app.clients.deepseek import DeepSeekClient
    client = DeepSeekClient()

    # 预取完整片段文本
    all_sids = set()
    for q in merged:
        for c in q.get('citations', []):
            if c.get('segment_id'):
                all_sids.add(c['segment_id'])
    transcript_map = _fetch_full_transcripts(all_sids)

    async def judge_one(q: Dict):
        if q['type'] == 'oos' or not q.get('reference_answer') or q['answer'].startswith('ERROR'):
            return None
        contexts = []
        for c in q.get('citations', []):
            sid = c.get('segment_id')
            if sid and transcript_map.get(sid):
                contexts.append(transcript_map[sid][:800])
        if not contexts:
            return None
        try:
            # faithfulness: 拆论断 → 核查
            claims_resp = await client.acall_llm(CLAIMS_PROMPT.format(answer=q['answer'][:2000]))
            claims = _extract_json(claims_resp).get('claims', [])
            if not claims:
                return None
            verify_resp = await client.acall_llm(VERIFY_PROMPT.format(
                contexts='\n\n'.join(contexts),
                claims='\n'.join(f"{i+1}. {c}" for i, c in enumerate(claims))))
            supported = _extract_json(verify_resp).get('supported', [])
            faith = sum(supported) / len(supported) if supported else None

            # relevancy
            rel_resp = await client.acall_llm(RELEVANCY_PROMPT.format(
                question=q['question'], answer=q['answer'][:2000]))
            rel = _extract_json(rel_resp).get('score')

            return {'faithfulness': faith, 'answer_relevancy': rel}
        except Exception as e:
            print(f"  judge failed {q['id']}: {e}")
            return None

    results = [r for r in await asyncio.gather(*[judge_one(q) for q in merged]) if r]
    if not results:
        return {}
    faith_vals = [r['faithfulness'] for r in results if r.get('faithfulness') is not None]
    rel_vals = [r['answer_relevancy'] for r in results if r.get('answer_relevancy') is not None]
    return {
        'faithfulness': sum(faith_vals) / len(faith_vals) if faith_vals else None,
        'answer_relevancy': sum(rel_vals) / len(rel_vals) if rel_vals else None,
    }


def _extract_json(resp: str) -> dict:
    if '```json' in resp:
        resp = resp.split('```json')[1].split('```')[0].strip()
    elif '```' in resp:
        resp = resp.split('```')[1].split('```')[0].strip()
    return json.loads(resp)


async def run_ragas(merged: List[Dict]):
    """RAGAS context_recall（collections 新版 API：ascore 逐样本打分）"""
    from ragas.metrics.collections import ContextRecall
    from ragas.llms import llm_factory
    from ragas.embeddings import embedding_factory
    from app.core.runtime_config import runtime_config

    # 收集所有引用片段 id，批量取完整文本
    all_sids = set()
    for q in merged:
        for c in q.get('citations', []):
            if c.get('segment_id'):
                all_sids.add(c['segment_id'])
    transcript_map = _fetch_full_transcripts(all_sids)

    samples = []
    for q in merged:
        if q['type'] == 'oos' or not q.get('reference_answer'):
            continue
        if q['answer'].startswith('ERROR'):
            continue
        # 用完整片段文本（前 800 字）替代 100 字摘要，裁判才能核对论断
        contexts = []
        for c in q.get('citations', []):
            sid = c.get('segment_id')
            if sid and transcript_map.get(sid):
                contexts.append(transcript_map[sid][:800])
        if not contexts:
            continue
        samples.append({
            'user_input': q['question'],
            'response': q['answer'],
            'retrieved_contexts': contexts,
            'reference': q['reference_answer']
        })

    if not samples:
        print("没有可用于 RAGAS 的样本")
        return {}

    print(f"\nRAGAS 评测 {len(samples)} 个样本...")
    cfg = runtime_config.get('llm')
    llm = llm_factory(cfg['model'], provider='openai',
                      client=_make_openai_client(cfg))

    # faithfulness/answer_relevancy 走自定义裁判（ragas 的 instructor 结构化输出与 DeepSeek 不兼容）
    # context_recall 用 ragas（它简单格式可以跑通）
    ctx_recall = ContextRecall(llm=llm)

    async def score_one(s):
        return await ctx_recall.ascore(user_input=s['user_input'],
                                       retrieved_contexts=s['retrieved_contexts'],
                                       reference=s['reference'])

    results = await asyncio.gather(*[score_one(s) for s in samples], return_exceptions=True)

    # 自定义裁判（faithfulness + answer_relevancy）
    custom = await custom_judges(merged)

    recall_vals = [r.value for r in results if not isinstance(r, Exception) and hasattr(r, 'value')]
    failed = len(results) - len(recall_vals)
    if failed:
        print(f"  context_recall 失败样本: {failed}")

    return {
        'faithfulness': custom.get('faithfulness'),
        'answer_relevancy': custom.get('answer_relevancy'),
        'context_recall': sum(recall_vals) / len(recall_vals) if recall_vals else None,
    }


def _make_openai_client(cfg):
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'])


POINT_COVERAGE_PROMPT = """你是评测裁判。判断"答案"是否覆盖了每个"答案要点"。

要点允许同义改写/近似数值（如"268.8万吨"和"268万吨"算覆盖），但必须语义等价，数值不能只字不同。

问题: {question}
答案: {answer}
答案要点:
{points}

只返回 JSON: {{"covered": [true/false, ...]}}（与要点一一对应）"""


async def judge_point_coverage(merged: List[Dict]) -> float:
    """LLM 裁判：答案要点覆盖率（替代字符串匹配，允许同义改写）"""
    from app.clients.deepseek import DeepSeekClient
    client = DeepSeekClient()

    async def judge_one(q: Dict) -> float:
        points = q.get('expected_answer_points', [])
        if not points:
            return None
        prompt = POINT_COVERAGE_PROMPT.format(
            question=q['question'], answer=q['answer'][:2000],
            points='\n'.join(f"{i+1}. {p}" for i, p in enumerate(points)))
        try:
            resp = await client.acall_llm(prompt)
            if '```json' in resp:
                resp = resp.split('```json')[1].split('```')[0].strip()
            elif '```' in resp:
                resp = resp.split('```')[1].split('```')[0].strip()
            covered = json.loads(resp)['covered']
            return sum(covered) / len(points) if covered else 0
        except Exception as e:
            print(f"  judge failed for {q['id']}: {e}")
            return None

    tasks = [judge_one(q) for q in merged
             if q['type'] != 'oos' and q.get('expected_answer_points') and not q['answer'].startswith('ERROR')]
    scores = [s for s in await asyncio.gather(*tasks) if s is not None]
    return sum(scores) / len(scores) if scores else 0


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--skip-ragas', action='store_true')
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    with open(DATASET_PATH) as f:
        dataset = json.load(f)

    # 1. 跑问答（带缓存）
    answers = collect_answers(dataset, args.limit)
    merged = merge_dataset(dataset, answers)

    # 2. L3：行为层
    valid = [q for q in merged if not q['answer'].startswith('ERROR')]
    oos = [q for q in merged if q['type'] == 'oos']
    refusal_rate = sum(1 for q in oos if check_refusal(q)) / len(oos) if oos else 0
    avg_tool_calls = sum(len(q.get('tool_calls', [])) for q in valid) / len(valid) if valid else 0
    avg_duration = sum(q.get('duration_ms', 0) for q in valid) / len(valid) if valid else 0

    print(f"\n{'='*64}")
    print(f"L3 行为层: 越界拒答率 {refusal_rate:.1%} ({sum(1 for q in oos if check_refusal(q))}/{len(oos)})")
    print(f"          平均工具调用 {avg_tool_calls:.1f} 次 | 平均耗时 {avg_duration/1000:.1f}s")

    # 3. L2 自定义：答案要点覆盖率（LLM 裁判）
    cite_acc = await judge_point_coverage(merged)
    print(f"\nL2 答案要点覆盖率（LLM 裁判）: {cite_acc:.1%}")

    # 4. L2 RAGAS
    ragas_result = {}
    if not args.skip_ragas:
        try:
            ragas_result = await run_ragas(merged)
            if ragas_result:
                print(f"\nRAGAS 结果:")
                print(ragas_result)
        except Exception as e:
            print(f"\nRAGAS 评测失败: {e}")

    # 5. 保存报告
    if args.save:
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        report_dir = REPORT_DIR / ts
        report_dir.mkdir(parents=True, exist_ok=True)
        l2_summary = {
            'refusal_rate': refusal_rate,
            'avg_tool_calls': avg_tool_calls,
            'avg_duration_s': avg_duration / 1000,
            'answer_point_coverage': cite_acc,
            'ragas': {k: float(v) for k, v in ragas_result.items() if v is not None} if ragas_result else {}
        }
        (report_dir / 'l2_report.json').write_text(json.dumps(
            {'timestamp': ts, 'summary': l2_summary, 'details': merged}, ensure_ascii=False, indent=2))
        print(f"\n报告已保存: {report_dir / 'l2_report.json'}")


if __name__ == '__main__':
    asyncio.run(main())
