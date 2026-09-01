"""
关系冲突检测接口
"""
import time
from typing import List, Dict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.conflict_detector import ConflictDetector
from app.core.logging import logger, log_request, log_response, log_performance

router = APIRouter()


class ConflictDetectRequest(BaseModel):
    segments: List[Dict]
    canonical_entities: List[Dict]


class ConflictDetectResponse(BaseModel):
    canonical_relationships: List[Dict]
    duplicate_groups: List[Dict]
    statistics: Dict


@router.post("/detect", response_model=ConflictDetectResponse)
async def detect_conflicts(request: ConflictDetectRequest):
    """
    关系冲突检测
    """
    start_time = time.time()

    log_request("/api/v1/conflict/detect", {
        "segments": len(request.segments),
        "canonical_entities": len(request.canonical_entities)
    })

    try:
        detector = ConflictDetector()
        result = await detector.detect(request.segments, request.canonical_entities)

        duration_ms = (time.time() - start_time) * 1000

        log_response("/api/v1/conflict/detect", 200, {
            "canonical_relationships": result['statistics']['active'],
            "duplicates": result['statistics']['duplicates']
        })
        log_performance("/api/v1/conflict/detect", duration_ms, {
            "canonical_relationships": result['statistics']['active'],
            "duplicates": result['statistics']['duplicates']
        })

        return ConflictDetectResponse(**result)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/conflict/detect", 500, error=str(e))
        log_performance("/api/v1/conflict/detect", duration_ms, {"error": str(e)})

        logger.error(f"Failed to detect conflicts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
