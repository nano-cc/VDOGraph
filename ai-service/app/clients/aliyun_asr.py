"""
阿里云 ASR 客户端
与 Java 端 AliyunAsrUtils 保持一致
"""
import requests
import time
from pathlib import Path
from typing import Optional
from app.core.config import settings
from app.core.logging import logger


class AliyunAsrClient:
    MAX_ATTEMPTS = 3

    def __init__(self):
        pass  # 配置改为运行时读取（Redis > .env）

    def _config(self):
        from app.core.runtime_config import runtime_config
        return runtime_config.get('asr')

    async def audio_to_text(self, audio_file: str) -> Optional[str]:
        """
        调用阿里云 ASR 转写音频
        与 Java 端 AliyunAsrUtils.audioToText() 保持一致
        """
        return self.audio_to_text_sync(audio_file)

    def audio_to_text_sync(self, audio_file: str) -> Optional[str]:
        """同步版本（内部全是同步 IO，供 to_thread 并发调用）"""
        file_path = Path(audio_file)
        if not file_path.is_file():
            raise ValueError(f"ASR audio file does not exist: {audio_file}")

        last_error = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                text = self._execute(file_path)
                if not text or not text.strip():
                    raise ValueError("ASR 返回空文本")
                return text.strip()
            except Exception as e:
                last_error = e
                logger.warning(f"ASR attempt failed: attempt={attempt + 1}, file={file_path.name}, error={e}")
                if attempt < self.MAX_ATTEMPTS - 1:
                    self._wait_before_retry(attempt)

        raise RuntimeError(f"ASR 调用失败，已达到最大重试次数: {last_error}")

    def _execute(self, file_path: Path) -> str:
        """
        执行 ASR 请求（base_url 为完整转写端点）
        与 Java 端 AliyunAsrUtils.execute() 保持一致
        """
        cfg = self._config()
        with open(file_path, 'rb') as f:
            files = {
                'file': (file_path.name, f, 'application/octet-stream')
            }
            data = {
                'model': cfg['model']
            }
            headers = {
                'Authorization': f"Bearer {cfg['api_key']}"
            }

            response = requests.post(
                cfg['base_url'],
                files=files,
                data=data,
                headers=headers,
                timeout=180  # 3 分钟超时
            )

            if response.status_code == 200:
                result = response.json()
                return result.get('text', '')

            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(f"ASR transient HTTP {response.status_code}")

            # 非 429/5xx 的失败源于请求本身（参数、鉴权、音频格式不支持），重投多少次都不会变好
            raise ValueError(f"ASR request rejected with HTTP {response.status_code}")

    def _wait_before_retry(self, attempt: int):
        """
        指数退避
        与 Java 端 AliyunAsrUtils.waitBeforeRetry() 保持一致
        """
        time.sleep(1 << attempt)  # 1s, 2s, 4s
