"""
视频解析接口
"""
import time
from fastapi import APIRouter, HTTPException

from app.models.video import VideoParseRequest, VideoParseResponse
from app.services.video_parser import VideoParser
from app.core.logging import logger, log_request, log_response, log_performance

router = APIRouter()


@router.post("/parse", response_model=VideoParseResponse)
async def parse_video(request: VideoParseRequest):
    """
    视频解析
    """
    start_time = time.time()

    log_request("/api/v1/video/parse", {
        "video_path": request.video_path,
        "user_goal": request.user_goal
    })

    try:
        parser = VideoParser()
        context = await parser.parse(request.video_path, request.user_goal)

        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/video/parse", 200, {
            "segments": len(context.segments)
        })
        log_performance("/api/v1/video/parse", duration_ms, {
            "segments": len(context.segments)
        })

        return VideoParseResponse(context=context)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_response("/api/v1/video/parse", 500, error=str(e))
        log_performance("/api/v1/video/parse", duration_ms, {"error": str(e)})

        logger.error(f"Failed to parse video: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
