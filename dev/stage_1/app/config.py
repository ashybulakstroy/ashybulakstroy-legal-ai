from pathlib import Path

from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./law_kz.db"
    secret_key: str = "change-me-in-production"
    debug: bool = True

    ai_provider: str
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout: int = 15

    openai_api_key: str = "not-needed"
    openai_base_url: str
    openai_timeout: int = 60

    llm_provider: str
    llm_model: str
    llm_client_id: str = "law-kz-app"

    mcp_server_name: str = "law-kz-mcp"

    cache_version: int = 1
    cache_ttl_days: int = 30

    model_config = {"env_file": _PROJECT_ROOT / ".env", "env_file_encoding": "utf-8"}


settings = Settings()
