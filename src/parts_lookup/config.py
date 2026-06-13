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
    # Document-selection breadth (#30): the fused-truncation that picks which
    # docs/pages seed the candidate set BEFORE within-doc page expansion. Raised
    # 3 -> 5 (#29): with many near-duplicate year/model manuals carrying
    # identical specs, the *named* doc routinely lands at fused rank 4-10 and was
    # discarded before Claude saw it. 5 over-fetches so the title rerank +
    # extraction can recover it. NOTE: this is no longer the total page count
    # sent to Claude — `retrieval_max_candidates` is the hard page budget after
    # neighbor expansion. Each candidate is one PDF page image to Claude vision.
    retrieval_top_k: int = Field(default=5, ge=1, le=10)
    # Within-document page-neighbor expansion (#30). The "right manual, wrong
    # page" failure: the leading manual is retrieved but the specific value page
    # lands just outside the candidate set. We expand ONLY the single top-RRF
    # document, anchored on its highest-fused page, adding `±retrieval_page_window`
    # neighbor pages (ordinal ± W) so the answer-bearing page reaches Claude.
    retrieval_page_window: int = Field(default=1, ge=0, le=3)
    # Doc-level page budget (#49, was #30): the maximum candidates (pages) sent
    # to Claude vision per query, AFTER leading-doc neighbor expansion. Decouples
    # "documents to consider" (`retrieval_top_k`) from "pages sent to Claude" so
    # expansion cost stays bounded. Raised 6 -> 10 (#49): #48 measured the
    # bottleneck as within-document page localization on the leading manual we
    # already pick (doc-recall ~52%, page-recall ~18%), so spending a larger page
    # budget on the ONE leading doc's pages is the single biggest lever to get
    # the answer-bearing page in front of Claude. ~10 PDF pages ≈ ~$0.05/query ≈
    # ~$0.50/mo at ~10 q/day, inside the CLAUDE.md ~$10/mo Claude budget. Base
    # fused-winner pages are never evicted; neighbors are appended
    # closest-to-anchor-first and dropped farthest-first when over budget (a
    # `retrieval.page_budget_drop` log fires when any page is dropped at the cap).
    retrieval_max_candidates: int = Field(default=10, ge=1, le=10)
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
    # Output-token ceiling for the extraction call. A vision answer that echoes
    # verbatim multi-unit torque strings plus the JSON envelope can exceed the
    # old 1024 limit and truncate mid-object (see #25 eval cases 1065/1294).
    # Tunable without a redeploy via EXTRACTION_MAX_TOKENS.
    extraction_max_tokens: int = Field(default=2048, ge=256, le=8192)
    # Resilience knobs for the Claude call (#33). The SDK's built-in bounded
    # exponential backoff honors Retry-After and only retries transient
    # failures (429/529/5xx/conn) — billing/auth/parse are never retried.
    # max_retries defaults to the SDK's own DEFAULT_MAX_RETRIES (2).
    extraction_max_retries: int = Field(default=2, ge=0, le=8)
    extraction_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    # Cap on concurrent in-flight vision calls so a request burst queues rather
    # than stampeding Anthropic's rate limit (asyncio.Semaphore, stdlib).
    extraction_max_concurrency: int = Field(default=4, ge=1, le=32)

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
