"""Application configuration loaded from environment variables or .env."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Unified settings for LLM inference, storage backends, auth, and runtime guardrails."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_locale: Literal["en", "zh"] = Field(
        default="en",
        description="Default UI, seed data, and knowledge base locale ('en' or 'zh').",
    )
    storage_backend: Literal["sqlite", "postgres"] = Field(
        default="sqlite",
        description="Persistence backend ('sqlite' for zero-config local, 'postgres' for pgvector cloud).",
    )
    postgres_dsn: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/ops_copilot",
        description="PostgreSQL connection DSN used when storage_backend='postgres'.",
    )

    auth_mode: Literal["token", "demo_rbac"] = Field(
        default="token",
        description="'token' enforces signed Bearer tokens; 'demo_rbac' allows request-body role fallback.",
    )
    auth_secret: str = Field(
        default="dev-ops-copilot-secret-change-in-prod",
        description="HMAC-SHA256 secret used to sign and verify stateless Bearer tokens.",
    )
    allow_demo_tokens: bool = Field(
        default=True,
        description="Whether POST /api/auth/demo-token is enabled for interactive evaluation.",
    )

    llm_base_url: str = Field(
        default="http://127.0.0.1:11434/v1",
        description="OpenAI-compatible base URL (Ollama, vLLM, or OpenAI cloud).",
    )
    llm_api_key: str = Field(
        default="ollama",
        description="API key; Ollama accepts any non-empty placeholder.",
    )
    llm_model: str = Field(
        default="qwen3:8b",
        description="Default chat/reasoning model identifier.",
    )
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=4096, ge=64)
    llm_timeout_seconds: float = Field(default=120.0, gt=0.0)
    ollama_num_ctx: int | None = Field(
        default=16384,
        ge=1024,
        description="Expected server-side OLLAMA_CONTEXT_LENGTH for local Ollama endpoints.",
    )
    embedding_model: str = Field(
        default="bge-m3",
        description="Embedding model identifier served via /v1/embeddings.",
    )

    rag_db_path: str = Field(
        default="data/knowledge.db",
        description="SQLite database path for ACL-aware hybrid RAG index.",
    )
    ops_db_path: str = Field(
        default="data/ops.db",
        description="SQLite database path for internal incidents, services, and deployments.",
    )
    checkpoint_db_path: str = Field(
        default="data/checkpoints.db",
        description="SQLite database path for HITL execution checkpoints.",
    )
    trace_db_path: str = Field(
        default="data/traces.db",
        description="SQLite database path for full-lifecycle audit traces.",
    )

    router_fast_model: str = Field(
        default="qwen3:8b",
        description="Fast low-latency model for simple queries and routing.",
    )
    router_strong_model: str = Field(
        default="qwen3:14b",
        description="Stronger model for complex multi-step troubleshooting and changes.",
    )
    rag_top_k: int = Field(default=4, ge=1, le=20)
    rag_max_context_chars: int = Field(default=3500, ge=500)

    otel_export_jsonl_path: str | None = Field(
        default=None,
        description="Optional path to append W3C/OTel-formatted JSON span records.",
    )
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        description="Optional OTLP/HTTP JSON traces endpoint (e.g. Jaeger, Tempo, Langfuse).",
    )

    def resolve_knowledge_dir(self) -> Path:
        """Select locale-appropriate knowledge base directory with graceful fallback."""
        if self.app_locale == "en":
            en_dir = Path("knowledge_base_en")
            if en_dir.exists():
                return en_dir
        return Path("knowledge_base")


def get_settings() -> Settings:
    """Return a fresh Settings instance."""
    return Settings()
