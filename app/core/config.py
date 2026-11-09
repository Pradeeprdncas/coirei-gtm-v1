from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'postgresql+asyncpg://postgres:password@localhost:5432/coirei_mvp'
    jwt_secret: str = 'REPLACE_WITH_32_PLUS_RANDOM_CHARACTERS_BEFORE_RUNNING'
    jwt_expire_minutes: int = 1440
    data_dir: Path = Path('data')
    cors_origins: str = 'http://localhost:5173,http://127.0.0.1:5173'
    ollama_base_url: str = 'http://127.0.0.1:11434/api'
    ollama_model: str = 'qwen2.5:3b-instruct'
    ollama_api_key: str = '5ba8efe8d9b34a97a594f9179dff255e.K4S63-m9m1I4CaHR8q2R_zTW'
    llm_optional: bool = True
    scrape_max_pages: int = 12
    scrape_timeout_seconds: int = 12
    scrape_max_bytes: int = 2_000_000
    tavily_api_key: str = ""
    brave_api_key: str = ""
    competitor_search_provider: str = "tavily"

    competitor_internal_url: str = (
        "http://127.0.0.1:8001"
    )
    competitor_internal_token: str = ""

    competitor_max_queries: int = 5
    competitor_max_candidates: int = 10
    competitor_max_pages: int = 4


@lru_cache

def settings() -> Settings:
    return Settings()



    
