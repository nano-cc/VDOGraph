"""
FastAPI 入口
"""
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.auth import verify_api_key
from app.core.config import settings

app = FastAPI(
    title="DoVideo AI Service",
    description="AI/ML algorithms for video content understanding",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由（/api/v1 下所有接口需要 API Key，/health 和 /docs 保持开放）
app.include_router(api_router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])

@app.get("/")
async def root():
    return {"message": "DoVideo AI Service"}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
