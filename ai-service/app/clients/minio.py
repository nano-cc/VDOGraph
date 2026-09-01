"""
MinIO 客户端
"""
from minio import Minio
from app.core.config import settings
from app.core.logging import logger


class MinioClient:
    def __init__(self):
        self.client = Minio(
            settings.minio_endpoint.replace("http://", "").replace("https://", ""),
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_endpoint.startswith("https")
        )
        self.bucket = settings.minio_bucket

    async def upload_file(self, file_path: str, file_name: str, prefix: str = "") -> str:
        """
        上传文件到 MinIO
        """
        return self.upload_file_sync(file_path, file_name, prefix)

    def upload_file_sync(self, file_path: str, file_name: str, prefix: str = "") -> str:
        """同步版本（内部是同步 SDK 调用，供 to_thread 并发调用）"""
        object_name = f"{prefix}/{file_name}" if prefix else file_name

        try:
            self.client.fput_object(
                self.bucket,
                object_name,
                file_path
            )

            # 返回文件 URL
            url = f"{settings.minio_endpoint}/{self.bucket}/{object_name}"
            logger.info(f"Uploaded file to MinIO: {url}")
            return url

        except Exception as e:
            logger.error(f"Failed to upload file to MinIO: {e}")
            raise
