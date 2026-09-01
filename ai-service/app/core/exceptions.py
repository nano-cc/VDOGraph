"""
异常处理
"""
from fastapi import HTTPException


class BusinessException(HTTPException):
    def __init__(self, code: int, message: str):
        super().__init__(status_code=code, detail=message)
