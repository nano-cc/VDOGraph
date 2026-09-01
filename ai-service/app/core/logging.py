"""
日志配置
"""
import logging
import sys
from pathlib import Path

# 创建日志目录
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

# 配置日志格式
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# 配置根日志记录器
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[
        # 控制台输出
        logging.StreamHandler(sys.stdout),
        # 文件输出
        logging.FileHandler(log_dir / "ai-service.log", encoding='utf-8')
    ]
)

# 创建日志记录器
logger = logging.getLogger(__name__)


def log_request(endpoint: str, request_data: dict):
    """记录请求"""
    logger.info(f"[REQUEST] {endpoint} - {request_data}")


def log_response(endpoint: str, status_code: int, response_data: dict = None, error: str = None):
    """记录响应"""
    if error:
        logger.error(f"[RESPONSE] {endpoint} - Status: {status_code} - Error: {error}")
    else:
        logger.info(f"[RESPONSE] {endpoint} - Status: {status_code} - Data: {response_data}")


def log_performance(endpoint: str, duration_ms: float, details: dict = None):
    """记录性能"""
    logger.info(f"[PERFORMANCE] {endpoint} - Duration: {duration_ms:.2f}ms - Details: {details}")


def log_step(step_name: str, details: dict = None):
    """记录步骤"""
    logger.info(f"[STEP] {step_name} - {details}")


def log_error(error: Exception, context: dict = None):
    """记录错误"""
    logger.error(f"[ERROR] {type(error).__name__}: {str(error)} - Context: {context}", exc_info=True)
