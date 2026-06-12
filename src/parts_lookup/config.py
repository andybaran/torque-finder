"""Environment-driven configuration.

Values are loaded once at process start. Everything that needs a credential
or a tunable knob takes a ``Settings`` instance through dependency injection
rather than reading the environment itself — keeps the bounded contexts pure
and the tests trivial.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    retrieval_top_k: int = Field(default=3, ge=1, le=10)
    # When true, VoyageEmbedder + ClaudeExtractor return deterministic
    # canned data instead of calling the real APIs. Local-smoke-test only.
    stub_external_apis: bool = False
    # Browser origins allowed to call the API (CORS). Comma-separated in the
    # env (CORS_ALLOW_ORIGINS); empty default = no cross-origin access.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_csv_origins(cls, v: object) -> object:
        # NoDecode disables pydantic-settings' JSON pre-decode for this field, so the raw
        # CORS_ALLOW_ORIGINS string reaches this validator. Split CSV -> list; pass an actual
        # list through unchanged; None -> empty list. Default (unset env) stays [].
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        if v is None:
            return []
        return v

    # --- Anthropic ---
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-sonnet-4-6"

    # --- Voyage ---
    voyage_api_key: SecretStr = SecretStr("")
    voyage_model: str = "voyage-3"
    voyage_dim: int = 1024

    # --- Cloudflare R2 (or any S3-compatible service) ---
    r2_account_id: str = ""
    r2_access_key_id: SecretStr = SecretStr("")
    r2_secret_access_key: SecretStr = SecretStr("")
    r2_bucket: str = "parts-lookup"
    r2_public_base_url: str | None = None
    # Set to point the S3 client at a non-Cloudflare endpoint (e.g. local MinIO).
    r2_endpoint_url_override: str | None = None

    # --- Postgres ---
    database_url: str

    # --- OpenTelemetry ---
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: str | None = None
    otel_service_name: str = "parts-lookup"

    # --- Sentry ---
    sentry_dsn: str | None = None

    # --- Discovery (SRAM crawl) ---
    sram_base_url: str = "https://www.sram.com"
    sram_docs_base_url: str = "https://docs.sram.com"
    discovery_user_agent: str = (
        "parts-lookup-discovery/0.1 (+https://github.com/andybaran/torque-finder)"
    )
    discovery_max_concurrency: int = Field(default=4, ge=1, le=16)
    discovery_request_delay_seconds: float = Field(default=0.5, ge=0.0)
    discovery_cache_dir: str = ".cache/discovery"

    @property
    def r2_endpoint_url(self) -> str:
        """S3-compatible endpoint for boto3 / aioboto3."""
        if self.r2_endpoint_url_override:
            return self.r2_endpoint_url_override
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Use as a FastAPI dependency."""
    return Settings()  # type: ignore[call-arg]
