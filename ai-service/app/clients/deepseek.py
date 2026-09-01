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
        """构建抽取 Prompt"""
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
