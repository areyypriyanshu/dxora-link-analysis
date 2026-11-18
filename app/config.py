"""Runtime configuration loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"

    fetch_timeout_seconds: float = 15.0
    llm_timeout_seconds: float = 90.0
    max_page_bytes: int = 2_000_000
    max_content_chars: int = 12_000
    max_redirects: int = 5
    user_agent: str = "LinkAnalysisBot/1.0 (practical-test; contact: hr@chivoxai.com)"


@lru_cache
def get_settings() -> Settings:
    return Settings()
