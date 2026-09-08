"""
配置管理
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DeepSeek API
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3.2"
    llm_timeout_seconds: int = 300

    # 分服务独立 key/base_url（2026-09-02：支持多供应商混用，如 LLM 走百炼、ASR 留 SF）
    # 留空则回退到 siliconflow_api_key / siliconflow_base_url（向后兼容）
    llm_api_key: str = ""
    llm_base_url: str = ""
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    asr_api_key: str = ""
    reranker_api_key: str = ""
    reranker_base_url: str = ""
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_path: str = "/rerank"  # 百炼 qwen3-rerank 用 /reranks（compatible-api）

    # ASR
    asr_url: str = "https://api.siliconflow.cn/v1/audio/transcriptions"
    asr_model: str = "TeleAI/TeleSpeechASR"

    # Embedding
    embedding_model: str = "BAAI/bge-m3"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    # MinIO
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "media"

    @property
    def deepseek_api_key(self) -> str:
        return self.siliconflow_api_key

    @property
    def deepseek_base_url(self) -> str:
        return self.siliconflow_base_url

    @property
    def deepseek_model(self) -> str:
        return self.llm_model

    @property
    def deepseek_timeout(self) -> int:
        return self.llm_timeout_seconds

    @property
    def ai_asr_url(self) -> str:
        return self.asr_url

    @property
    def ai_asr_model(self) -> str:
        return self.asr_model

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"  # 默认密码

    # 服务间认证（Java 后端调用本服务时携带）
    ai_service_api_key: str = ""

    # Java 后端地址（#66：本服务回调 Java /internal/kg/* 推进任务状态机）
    java_backend_url: str = "http://localhost:9090"
    # Python → Java 回调鉴权（对应 Java kg.internal.api-key；默认与服务间认证同 key）
    kg_internal_api_key: str = ""

    # 两跳消歧开关（docs/kg-phase4-commit-optimization.md）：true=commit 走 prepare/apply 新链路
    kg_two_hop: bool = False

    # gleaning 补抽轮数（GraphRAG 式：首轮抽取后让 LLM 自查遗漏，0=关闭）
    kg_gleaning_rounds: int = 1

    # global search map-reduce（GraphRAG 式：map 阶段逐社区提取与问题相关要点+打分，
    # reduce 阶段按分过滤组装，治"整段摘要灌进上下文"的污染问题；false=回退整段摘要）
    kg_global_map_reduce: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "allow"  # 允许额外的字段


settings = Settings()
