from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "mysql+pymysql://shijian:shijian@localhost:3306/shijian?charset=utf8mb4"
    redis_url: str = "redis://localhost:6379/0"
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "shijian_bge_small_zh_v1"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = ""
    deepseek_model: str = ""
    search_api_key: SecretStr = SecretStr("")
    search_provider: Literal["deepseek", "tavily", "wikisource"] = "deepseek"
    deepseek_tools_base_url: str = "https://api.deepseek.com"
    deepseek_tools_api_key: SecretStr = SecretStr("")
    search_price_cny_per_1000: Decimal | None = Field(None, ge=0)
    fetch_price_cny_per_1000: Decimal | None = Field(None, ge=0)
    max_web_searches: int = Field(3, ge=1, le=10)
    evaluation_dir: str = "data/evaluations"
    search_api_url: str = "https://api.tavily.com/search"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_revision: str = "7999e1d3359715c523056ef9478215996d62a620"
    embedding_dimension: int = 512
    embedding_device: str = "cpu"
    model_cache: str = "data/cache/models"
    max_rounds: int = Field(4, ge=1, le=10)
    max_tool_calls: int = Field(16, ge=1, le=40)
    max_web_pages: int = Field(4, ge=0, le=10)
    research_timeout: int = Field(240, ge=30, le=1800)
    request_timeout: int = Field(30, ge=1, le=120)
    fetch_max_bytes: int = Field(2_000_000, ge=1024, le=5_000_000)
    max_evidence: int = Field(24, ge=1, le=40)
    max_model_requests: int = Field(16, ge=1, le=100)
    max_model_tokens: int = Field(60000, ge=1, le=1000000)
    lease_seconds: int = Field(120, ge=30)
    heartbeat_seconds: int = Field(15, ge=1)

    def web_ready(self) -> bool:
        return self.web_error() is None

    def tools_key(self):
        return self.deepseek_tools_api_key.get_secret_value() or self.deepseek_api_key.get_secret_value()

    def web_error(self):
        if self.search_provider == "deepseek":
            if not self.deepseek_tools_base_url.strip():
                return "缺少 DEEPSEEK_TOOLS_BASE_URL"
            if not self.tools_key():
                return "DeepSeek联网工具缺少 DEEPSEEK_API_KEY 或 DEEPSEEK_TOOLS_API_KEY"
        elif self.search_provider != "wikisource" and not self.search_api_key.get_secret_value():
            return "联网模式缺少 SEARCH_API_KEY"
        return None

    def model_error(self) -> str | None:
        missing = [
            k.upper()
            for k in ("deepseek_api_key", "deepseek_base_url", "deepseek_model")
            if not (getattr(self, k).get_secret_value() if k.endswith("key") else getattr(self, k))
        ]
        return "缺少配置：" + ", ".join(missing) if missing else None


@lru_cache
def settings() -> Settings:
    return Settings()
