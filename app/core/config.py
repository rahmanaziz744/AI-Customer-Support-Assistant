"""Application settings, loaded from the environment (and `.env` for local dev)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Anthropic ---------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    agent_model: str = "claude-opus-5"
    agent_effort: str = "medium"
    classifier_model: str = ""
    judge_model: str = ""

    # -- Database ----------------------------------------------------------
    postgres_user: str = "support"
    postgres_password: str = "support"
    postgres_db: str = "support_agent"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

    # -- Agent behaviour ---------------------------------------------------
    escalation_confidence_threshold: float = 0.65
    escalation_retrieval_threshold: float = 0.35
    max_auto_refund_usd: float = 500.0
    retrieval_top_k: int = 4

    # -- Embeddings --------------------------------------------------------
    # "fastembed" downloads a small ONNX model; "hash" is a deterministic
    # offline stand-in used by the test suite so CI needs no model download.
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # -- Observability -----------------------------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "support-agent"
    log_level: str = "INFO"
    log_format: str = "console"

    # -- API ---------------------------------------------------------------
    rate_limit_tickets: str = "20/minute"
    cors_origins: str = "http://localhost:5173"
    # The agent reaches the order system over HTTP even though it is mounted in
    # this same app, so swapping in a real commerce backend is a config change.
    order_api_base_url: str = "http://localhost:8000"
    order_api_timeout_seconds: float = 10.0

    policies_dir: Path = Field(default=REPO_ROOT / "data" / "policies")

    @computed_field
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy DSN (psycopg3 driver)."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def sync_database_url(self) -> str:
        """Sync DSN, used by Alembic and the LangGraph checkpointer."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def model_for(self, node: str) -> str:
        """Resolve the model for a node, falling back to the global default."""
        overrides = {"classify": self.classifier_model, "judge": self.judge_model}
        return overrides.get(node) or self.agent_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
