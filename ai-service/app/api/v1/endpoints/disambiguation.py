"""
实体消歧接口
"""
import time
from typing import List, Dict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.disambiguator import Disambiguator
from app.core.logging import logger, log_request, log_response, log_performance

router = APIRouter()


class DisambiguationRequest(BaseModel):
    segments: List[Dict]


class DisambiguationResponse(BaseModel):
    canonical_entities: List[Dict]
    merge_history: List[Dict]
    statistics: Dict


@router.post("/disambiguate", response_model=DisambiguationResponse)
async def disambiguate(request: DisambiguationRequest):
    """
    实体消歧
    """
    start_time = time.time()

    log_request("/api/v1/disambiguation/disambiguate", {
        "segments": len(request.segments)
    })

    try:
        disambiguator = Disambiguator()
        result = await disambiguator.disambiguate(request.segments)

        duration_ms = (time.time() - start_time) * 1000

        log_response("/api/v1/disambiguation/disambiguate", 200, {
            "canonical_entities": result['statistics']['canonical_count'],
            "merge_rate": result['statistics']['merge_rate']
        })
        log_performance("/api/v1/disambiguation/disambiguate", duration_ms, {
            "canonical_entities": result['statistics']['canonical_count'],
            "level1_hits": result['statistics']['level1_hits'],
            "level2_hits": result['statistics']['level2_hits'],
            "level3_hits": result['statistics']['level3_hits']
        })

        return DisambiguationResponse(**result)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/disambiguation/disambiguate", 500, error=str(e))
        log_performance("/api/v1/disambiguation/disambiguate", duration_ms, {"error": str(e)})

        logger.error(f"Failed to disambiguate entities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
