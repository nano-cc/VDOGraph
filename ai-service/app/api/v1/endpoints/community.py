"""
社区检测接口
"""
import time
from fastapi import APIRouter, HTTPException

from app.models.community import CommunityDetectRequest, CommunityDetectResponse
from app.services.community_detector import CommunityDetector
from app.core.logging import logger, log_request, log_response, log_performance

router = APIRouter()


@router.post("/detect", response_model=CommunityDetectResponse)
async def detect_communities(request: CommunityDetectRequest):
    """
    社区检测
    """
    start_time = time.time()

    log_request("/api/v1/community/detect", {
        "entities": len(request.entities),
        "relationships": len(request.relationships)
    })

    try:
        detector = CommunityDetector()
        result = await detector.detect(request.entities, request.relationships)

        duration_ms = (time.time() - start_time) * 1000

        log_response("/api/v1/community/detect", 200, {
            "communities": result['statistics']['total_communities'],
            "avg_community_size": result['statistics']['avg_community_size']
        })
        log_performance("/api/v1/community/detect", duration_ms, {
            "communities": result['statistics']['total_communities'],
            "avg_community_size": result['statistics']['avg_community_size']
        })

        return CommunityDetectResponse(**result)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/community/detect", 500, error=str(e))
        log_performance("/api/v1/community/detect", duration_ms, {"error": str(e)})

        logger.error(f"Failed to detect communities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
