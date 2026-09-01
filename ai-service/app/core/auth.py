"""
API Key 认证（Python 服务只应被 Java 后端调用）
"""
from fastapi import Header, HTTPException
from app.core.config import settings


async def verify_api_key(x_api_key: str = Header(default="")):
    """校验 X-API-Key；未配置 key 时放行（本地开发兼容）"""
    expected = settings.ai_service_api_key
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
