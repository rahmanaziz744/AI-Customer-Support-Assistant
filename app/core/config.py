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
    agent_model: str = "claude-sonnet-5"
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
    # Multiple windows, semicolon-separated, are enforced together: the minute
    # window stops bursts, the day window stops a slow drip that would still
    # drain the model budget overnight.
    rate_limit_tickets: str = "20/minute;300/hour;1000/day"
    # Applied to the routes that trigger model work on an existing ticket
    # (synchronous processing and approval), which are more expensive per call
    # than submission.
    rate_limit_expensive: str = "10/minute;100/hour;400/day"
    # Fallback for every route without its own limit. Generous by design — it
    # exists to stop a read endpoint being hammered, not to shape normal use.
    # Empty disables it.
    rate_limit_default: str = "240/minute"
    # How many proxies sit in front of this process and append to
    # `X-Forwarded-For`. 0 when exposed directly, 2 behind the deployed
    # Caddy -> nginx chain. See app.core.rate_limit.client_ip.
    trusted_proxy_hops: int = 0
    cors_origins: str = "http://localhost:5173"
    # The agent reaches the order system over HTTP even though it is mounted in
    # this same app, so swapping in a real commerce backend is a config change.
    order_api_base_url: str = "http://localhost:8000"
    order_api_timeout_seconds: float = 10.0

    # -- Cost controls -----------------------------------------------------
    # Rolling 24h ceiling on model spend, in USD. 0 disables enforcement, which
    # is the default so local dev and the test suite are unaffected; a public
    # deployment must set it. See app.core.budget.
    daily_budget_usd: float = 0.0
    # Fraction of the ceiling that counts as "warn" — surfaced on the budget
    # snapshot log line so a CloudWatch alarm can fire before work is refused.
    budget_warn_ratio: float = 0.6
    # How long a spend reading is reused before re-querying. Trades a little
    # overshoot past the ceiling for not running an aggregate per request.
    budget_cache_seconds: float = 15.0
    # Interval between `budget_snapshot` log lines. 0 disables the loop.
    budget_snapshot_seconds: float = 60.0
    # Ceiling on agent runs executing at once. Submission is cheap and returns
    # immediately, so without this a burst of accepted tickets becomes a burst
    # of concurrent graph runs.
    max_concurrent_runs: int = 2

    # -- Demo protection ---------------------------------------------------
    # Shared secret for the destructive routes on a public demo. Empty (the
    # default) leaves them open, which is what local dev and tests want.
    demo_admin_token: SecretStr = SecretStr("")

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
