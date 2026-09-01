"""
实体关系抽取服务
"""
import time
import asyncio
from typing import List
from app.models.entity import SegmentExtraction, Entity, Relationship
from app.clients.deepseek import DeepSeekClient
from app.core.logging import logger


class EntityExtractor:
    EXTRACT_CONCURRENCY = 5  # 抽取并发数（片段间零共享状态）

    def __init__(self):
        self.deepseek_client = DeepSeekClient()

    async def extract(self, segments: List[SegmentExtraction], on_segment_done=None) -> List[SegmentExtraction]:
        """
        抽取实体和关系（有界并发：片段间完全独立，acall_llm 内部已做限流+重试+线程池）
        on_segment_done: 可选协程回调（每片段完成时调用，用于进度上报）
        """
        start_time = time.time()
        logger.info(f"[ENTITY_EXTRACT] Starting extraction for {len(segments)} segments, concurrency={self.EXTRACT_CONCURRENCY}")

        semaphore = asyncio.Semaphore(self.EXTRACT_CONCURRENCY)

        async def extract_one(i: int, segment: SegmentExtraction) -> SegmentExtraction:
            segment_start = time.time()
            async with semaphore:
                logger.info(f"[ENTITY_EXTRACT] Processing segment {i+1}/{len(segments)}: {segment.segment_id}")
                try:
                    # 构建输入文本
                    input_text = self._build_input_text(segment)
                    logger.info(f"[ENTITY_EXTRACT] Segment {i+1} input text length: {len(input_text)} chars")

                    # 调用 LLM 抽取
                    llm_start = time.time()
                    extraction_result = await self.deepseek_client.extract_entities(input_text)
                    llm_duration = (time.time() - llm_start) * 1000
                    logger.info(f"[ENTITY_EXTRACT] Segment {i+1} LLM call completed in {llm_duration:.2f}ms")

                    # 解析结果
                    entities, relationships = self._parse_extraction_result(extraction_result)
                    logger.info(f"[ENTITY_EXTRACT] Segment {i+1} parsed: {len(entities)} entities, {len(relationships)} relationships")

                    segment.entities = entities
                    segment.relationships = relationships

                    segment_duration = (time.time() - segment_start) * 1000
                    logger.info(f"[ENTITY_EXTRACT] Segment {i+1} completed in {segment_duration:.2f}ms")

                except Exception as e:
                    segment_duration = (time.time() - segment_start) * 1000
                    segment.extraction_failed = True  # 不缓存失败结果，下次重跑会重试
                    logger.error(f"[ENTITY_EXTRACT] Segment {i+1} failed after {segment_duration:.2f}ms: {type(e).__name__}: {str(e)}", exc_info=True)

                if on_segment_done is not None:
                    try:
                        await on_segment_done(i, segment)
                    except Exception as cb_err:
                        logger.warning(f"[ENTITY_EXTRACT] on_segment_done callback error: {cb_err}")
                return segment

        results = await asyncio.gather(*[extract_one(i, s) for i, s in enumerate(segments)])

        total_duration = (time.time() - start_time) * 1000
        total_entities = sum(len(s.entities) for s in results)
        total_relationships = sum(len(s.relationships) for s in results)

        logger.info(f"[ENTITY_EXTRACT] Extraction completed: {len(results)} segments, {total_entities} entities, {total_relationships} relationships, {total_duration:.2f}ms")

        return list(results)

    def _build_input_text(self, segment: SegmentExtraction) -> str:
        """构建输入文本"""
        text = f"[时间: {segment.start_ms//1000:02d}:{segment.start_ms%1000//10:02d}-{segment.end_ms//1000:02d}:{segment.end_ms%1000//10:02d}]\n"
        text += "[语音内容]\n"
        text += segment.transcript + "\n"

        if segment.ocr_texts:
            text += "\n[画面文字]\n"
            for ocr_text in segment.ocr_texts:
                text += ocr_text + "\n"

        return text

    def _parse_extraction_result(self, result: str) -> tuple:
        """解析 LLM 返回结果"""
        entities = []
        relationships = []

        lines = result.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line == '##' or line == '<|COMPLETE|>':
                continue
            # LLM 可能把分隔符粘在内容行末尾：("entity"...描述)## ← 去掉
            if line.endswith('##'):
                line = line[:-2].rstrip()

            if line.startswith('("entity"'):
                parts = line.split('<|>')
                if len(parts) >= 4:
                    entity = Entity(
                        name=parts[1].strip(),
                        type=parts[2].strip(),
                        description=parts[3].strip().rstrip(')'),
                        confidence=0.9,
                        source_type="asr"
                    )
                    entities.append(entity)

            elif line.startswith('("relationship"'):
                parts = line.split('<|>')
                if len(parts) >= 5:
                    # 强度字段鲁棒解析：LLM 可能输出 "8" / "8)" / "8)##" 等形式
                    strength_raw = parts[4].strip()
                    strength_digits = ''.join(c for c in strength_raw if c.isdigit())
                    if not strength_digits:
                        continue
                    relationship = Relationship(
                        source=parts[1].strip(),
                        target=parts[2].strip(),
                        description=parts[3].strip(),
                        strength=min(int(strength_digits), 10),
                        confidence=0.9,
                        source_type="asr"
                    )
                    relationships.append(relationship)

        return entities, relationships
