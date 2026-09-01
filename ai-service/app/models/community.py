"""
社区相关模型
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class CommunityEntity(BaseModel):
    id: str = Field(..., description="实体 ID")
    name: str = Field(..., description="实体名称")
    type: str = Field(..., description="实体类型")
    description: str = Field(default="", description="实体描述")
    source_count: int = Field(default=1, description="来源片段数")


class CommunityRelationship(BaseModel):
    source: str = Field(..., description="源实体")
    target: str = Field(..., description="目标实体")
    description: str = Field(..., description="关系描述")
    strength: int = Field(..., description="关系强度")


class Community(BaseModel):
    id: str = Field(..., description="社区 ID")
    level: int = Field(default=0, description="层级")
    parent_id: Optional[str] = Field(default=None, description="父社区 ID（层次化划分）")
    entity_count: int = Field(..., description="实体数")
    relationship_count: int = Field(..., description="关系数")
    entities: List[CommunityEntity] = Field(default_factory=list, description="实体列表")
    relationships: List[CommunityRelationship] = Field(default_factory=list, description="关系列表")
    source_segments: List[str] = Field(default_factory=list, description="来源片段")
    summary: Optional[str] = Field(default=None, description="社区摘要")


class CommunityDetectRequest(BaseModel):
    entities: List[dict] = Field(..., description="实体列表")
    relationships: List[dict] = Field(..., description="关系列表")


class CommunityDetectResponse(BaseModel):
    communities: List[Community] = Field(..., description="社区列表")
    statistics: dict = Field(..., description="统计信息")
