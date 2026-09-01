"""
实体关系抽取接口
"""
import time
from fastapi import APIRouter, HTTPException

from app.models.entity import EntityExtractRequest, EntityExtractResponse
from app.services.entity_extractor import EntityExtractor
from app.core.logging import logger, log_request, log_response, log_performance

router = APIRouter()


@router.post("/extract", response_model=EntityExtractResponse)
async def extract_entities(request: EntityExtractRequest):
    """
    实体关系抽取
    """
    start_time = time.time()

    log_request("/api/v1/entity/extract", {
        "segments": len(request.segments)
    })

    try:
        extractor = EntityExtractor()
        results = await extractor.extract(request.segments)

        duration_ms = (time.time() - start_time) * 1000
        total_entities = sum(len(s.entities) for s in results)
        total_relationships = sum(len(s.relationships) for s in results)

        log_response("/api/v1/entity/extract", 200, {
            "segments": len(results),
            "total_entities": total_entities,
            "total_relationships": total_relationships
        })
        log_performance("/api/v1/entity/extract", duration_ms, {
            "segments": len(results),
            "total_entities": total_entities,
            "total_relationships": total_relationships
        })

        return EntityExtractResponse(results=results)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/entity/extract", 500, error=str(e))
        log_performance("/api/v1/entity/extract", duration_ms, {"error": str(e)})

        logger.error(f"Failed to extract entities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
