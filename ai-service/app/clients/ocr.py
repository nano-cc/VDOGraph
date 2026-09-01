"""
Tesseract OCR 客户端
"""
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from app.core.logging import logger


class OcrClient:
    async def recognize(self, image_path: str) -> Optional[str]:
        """
        调用 Tesseract OCR 识别图片文字
        与 Java 端 OcrUtils.recognize() 保持一致
        """
        return self.recognize_sync(image_path)

    def recognize_sync(self, image_path: str) -> Optional[str]:
        """同步版本（内部是子进程，供 to_thread 并发调用）"""
        if not Path(image_path).is_file():
            raise ValueError(f"OCR image does not exist: {image_path}")

        # 创建临时输出文件
        output_base = tempfile.mktemp(prefix="dovideo-ocr-")

        try:
            # 调用 Tesseract（与 Java 端一致）
            process = subprocess.Popen(
                ["tesseract", image_path, output_base, "-l", "chi_sim+eng"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            process.wait(timeout=120)  # 2 分钟超时

            if process.returncode != 0:
                raise RuntimeError(f"OCR process failed with exit code {process.returncode}")

            # 读取结果
            output_file = Path(f"{output_base}.txt")
            if output_file.exists():
                result = output_file.read_text(encoding='utf-8').strip()
                output_file.unlink()
                return result

            return None

        except subprocess.TimeoutExpired:
            process.kill()
            raise RuntimeError("OCR execution timed out")
        except Exception as e:
            logger.error(f"OCR failed for {image_path}: {e}")
            raise
        finally:
            # 清理临时文件
            Path(f"{output_base}.txt").unlink(missing_ok=True)
