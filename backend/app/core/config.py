"""Application settings loaded from environment variables and ``backend/.env``."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SayDo"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Required: set in backend/.env (no defaults, so credentials never live in code).
    database_url: str
    test_database_url: str
    db_echo: bool = False

    upload_dir: Path = Path("uploads")
    max_upload_mb: int = Field(default=50, gt=0)

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # LLM (used from Prompt 3 onwards).
    groq_api_key: SecretStr | None = None

    # Embeddings. EMBEDDING_DIM must match the chunks.embedding column (vector(768)).
    gemini_api_key: SecretStr | None = None
    embedding_provider: Literal["gemini", "fake"] = "gemini"
    embedding_dim: int = Field(default=768, gt=0)
    # Minimum gap between embedding API requests (the Gemini free tier is rate limited).
    embedding_min_request_interval_s: float = Field(default=1.0, ge=0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("groq_api_key", "gemini_api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("upload_dir", mode="after")
    @classmethod
    def _resolve_upload_dir(cls, value: Path) -> Path:
        # Relative paths are anchored at backend/, so the CWD doesn't matter.
        return value if value.is_absolute() else (BACKEND_DIR / value).resolve()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
