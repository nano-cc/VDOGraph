"""
DeepSeek API 客户端
"""
import requests
from typing import Optional
from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import llm_limiter, call_with_retry


class DeepSeekClient:
    def __init__(self):
        # 配置改为运行时读取（Redis > .env），支持前端动态修改供应商
        self.timeout = 600

    def _config(self):
        from app.core.runtime_config import runtime_config
        return runtime_config.get('llm')

    async def extract_entities(self, input_text: str) -> str:
        """抽取实体和关系"""
        prompt = self._build_extraction_prompt(input_text)
        return await self.acall_llm(prompt)

    async def extract_entities_gleaning(self, input_text: str, previous_output: str) -> str:
        """gleaning 补抽（GraphRAG 式）：让 LLM 复查同一段文本，只输出遗漏的实体/关系"""
        prompt = f"""-Goal-
Entities and relationships were already extracted from a text. Review the SAME text carefully and identify entities or relationships that were MISSED.

-Text-
{input_text}

-Already extracted (do NOT repeat these)-
{previous_output}

-Instructions-
- First answer this question internally: 还有遗漏吗？只补"确实出现在文本中且没被抽到"的，不要为了凑数而输出。
- Output ONLY newly found entities/relationships, using the exact same format:
  ("entity"<|><entity_name><|><entity_type><|><entity_description>)
  ("relationship"<|><source_entity<|><target_entity><|><relationship_description><|><relationship_strength>)
- Use **##** as the list delimiter.
- Entity types allowed: [Person, Organization, Location, Product, Concept, Event, Other]
- entity_name/描述语言与原文一致（中文文本用中文）；代词（他/它/这家公司/该算法等）不是实体，要还原为具体名称再抽取。
- Do not repeat anything from the already-extracted list; do not output explanations.
- If nothing was missed, output only <|COMPLETE|>

Output:
"""
        return await self.acall_llm(prompt)

    async def generate_summary(self, prompt: str) -> str:
        """生成摘要"""
        return await self.acall_llm(prompt)

    def call_llm(self, prompt: str) -> str:
        """调用 LLM（公开方法，同步）"""
        return self._call_llm(prompt)

    async def acall_llm(self, prompt: str) -> str:
        """调用 LLM（异步：限流 + 退避重试 + 线程池执行，不阻塞事件循环）"""
        return await call_with_retry(self._call_llm, llm_limiter, prompt=prompt)

    def _build_extraction_prompt(self, input_text: str) -> str:
        """构建抽取 Prompt（2026-09-02 中文化修复：
        原版 GraphRAG prompt 的 "capitalized" + 英文 few-shot 导致 qwen 系模型
        把中文实体翻成大写英文（向量/全文检索对中文查询失效），且照抄示例实体 MARTIN SMITH）
        """
        return f"""-Goal-
Given a text document and a list of entity types, identify all entities of those types from the text and all relationships among the identified entities.

-Language Rules (IMPORTANT)-
1. entity_name MUST use the SAME language as the source text. 中文文本中的实体必须用中文名（如"法兰克福条约"），禁止翻译成英文或转成大写。
2. entity_description and relationship_description MUST also use the source text's language.
3. Only extract entities that ACTUALLY APPEAR in the text below. 禁止输出示例中出现的实体，禁止输出文本中不存在的内容。
4. Descriptions must be grounded in the given text only. 不要引入文本之外的背景知识。
5. 文本可能包含【画面文字】部分（视频画面的 OCR 识别结果）。画面文字的术语拼写比语音识别更可靠：当【语音内容】的某个词与【画面文字】的词读音相近而写法不同（语音同音误写），entity_name 必须采用【画面文字】的写法。例如语音写"Cloud Code"而画面写"Claude Code"时，实体名用"Claude Code"。
6. 代词（他/它/这家公司/该算法/这个国家等）不是实体：抽取时必须还原为文本中指代的具体名称；还原不了就不抽。

-Steps-
1. Identify all entities. For each identified entity, extract:
- entity_name: Name of the entity (same language as source text)
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
Entity_types: PERSON,ORGANIZATION,EVENT
Text:
1871年，法国政府在普法战争战败后与普鲁士签订了法兰克福条约，国民自卫军代表皮埃尔对此表示强烈抗议。
######################
Output:
("entity"<|>法兰克福条约<|>EVENT<|>1871年法国在普法战争战败后与普鲁士签订的和约)
##
("entity"<|>普鲁士<|>ORGANIZATION<|>普法战争的战胜方，与法国签订法兰克福条约)
##
("entity"<|>皮埃尔<|>PERSON<|>国民自卫军代表，对法兰克福条约表示强烈抗议)
##
("relationship"<|>法兰克福条约<|>普鲁士<|>法兰克福条约是法国与普鲁士签订的和约<|>9)
##
("relationship"<|>皮埃尔<|>法兰克福条约<|>皮埃尔对法兰克福条约表示强烈抗议<|>7)
<|COMPLETE|>

-Real Data-
Entity_types: Person, Organization, Location, Product, Concept, Event, Other
Text: {input_text}
Output:
"""

    def _call_llm(self, prompt: str) -> str:
        """调用 LLM"""
        cfg = self._config()
        url = f"{cfg['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }
        data = {
            "model": cfg['model'],
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "max_tokens": 2000
        }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=self.timeout)
            response.raise_for_status()

            result = response.json()
            return result['choices'][0]['message']['content']

        except Exception as e:
            logger.error(f"Failed to call DeepSeek API: {e}")
            raise
