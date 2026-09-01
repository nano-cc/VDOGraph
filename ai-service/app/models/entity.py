"""
实体相关模型
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class Entity(BaseModel):
    name: str = Field(..., description="实体名称")
    type: str = Field(..., description="实体类型")
    description: str = Field(default="", description="实体描述")
    confidence: float = Field(default=0.0, description="置信度")
    source_type: str = Field(default="asr", description="来源类型", alias="sourceType")

    class Config:
        populate_by_name = True


class Relationship(BaseModel):
    source: str = Field(..., description="源实体")
    target: str = Field(..., description="目标实体")
    description: str = Field(..., description="关系描述")
    strength: int = Field(..., description="关系强度")
    confidence: float = Field(default=0.0, description="置信度")
    source_type: str = Field(default="asr", description="来源类型", alias="sourceType")

    class Config:
        populate_by_name = True


class ExtractionResult(BaseModel):
    entities: List[Entity] = Field(default_factory=list, description="实体列表")
    relationships: List[Relationship] = Field(default_factory=list, description="关系列表")


class SegmentExtraction(BaseModel):
    segment_id: str = Field(..., description="片段 ID", alias="segmentId")
    segment_index: int = Field(..., description="片段索引", alias="segmentIndex")
    start_ms: int = Field(..., description="开始时间", alias="startMs")
    end_ms: int = Field(..., description="结束时间", alias="endMs")
    transcript: str = Field(..., description="ASR 文本")
    ocr_texts: List[str] = Field(default_factory=list, description="OCR 文本", alias="ocrTexts")
    entities: List[Entity] = Field(default_factory=list, description="实体列表")
    relationships: List[Relationship] = Field(default_factory=list, description="关系列表")
    # 抽取失败标记（不落库：失败的片段不写 raw_extraction，下次重试）
    extraction_failed: bool = Field(default=False, exclude=True)

    class Config:
        populate_by_name = True


class EntityExtractRequest(BaseModel):
    segments: List[SegmentExtraction] = Field(..., description="片段列表")


class EntityExtractResponse(BaseModel):
    results: List[SegmentExtraction] = Field(..., description="抽取结果")
