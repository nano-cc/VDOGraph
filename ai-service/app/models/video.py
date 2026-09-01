"""
视频相关模型
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class VideoSegment(BaseModel):
    start_ms: int = Field(..., description="开始时间（毫秒）")
    end_ms: int = Field(..., description="结束时间（毫秒）")
    transcript: str = Field(default="", description="ASR 文本")
    ocr_texts: List[str] = Field(default_factory=list, description="OCR 文本列表")
    evidence_frames: List[str] = Field(default_factory=list, description="关键帧 URL 列表")


class VideoContext(BaseModel):
    source: str = Field(..., description="视频源")
    user_goal: str = Field(default="", description="用户目标")
    segments: List[VideoSegment] = Field(default_factory=list, description="片段列表")


class VideoParseRequest(BaseModel):
    video_path: str = Field(..., description="视频路径")
    user_goal: str = Field(default="", description="用户目标")


class VideoParseResponse(BaseModel):
    context: VideoContext = Field(..., description="视频上下文")
