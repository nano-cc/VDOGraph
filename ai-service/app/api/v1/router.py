"""
API v1 路由汇总
"""
from fastapi import APIRouter

from app.api.v1.endpoints import video, entity, community, disambiguation, conflict, pipeline, query

api_router = APIRouter()

api_router.include_router(video.router, prefix="/video", tags=["video"])
api_router.include_router(entity.router, prefix="/entity", tags=["entity"])
api_router.include_router(community.router, prefix="/community", tags=["community"])
api_router.include_router(disambiguation.router, prefix="/disambiguation", tags=["disambiguation"])
api_router.include_router(conflict.router, prefix="/conflict", tags=["conflict"])
api_router.include_router(pipeline.router, prefix="/pipeline", tags=["pipeline"])
api_router.include_router(query.router, prefix="/query", tags=["query"])
