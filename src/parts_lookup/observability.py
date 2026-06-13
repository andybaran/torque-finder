"""Cross-cutting observability wiring: structlog + OpenTelemetry + Sentry.

Call ``setup_observability(settings)`` once at process start (typically from
the FastAPI app factory). Subsequent calls are no-ops so tests and reloaders
can re-enter without double-registering exporters.

Application code should obtain loggers via ``get_logger`` and the tracer via
``tracer`` — both work whether or not setup has run, so unit tests don't need
to bootstrap the full pipeline.
"""

from __future__ import annotations

import logging
import sys
from typing import Any
from urllib.parse import unquote

import structlog
from opentelemetry import trace

from parts_lookup.config import Settings

_initialised = False

# Module-level tracer. ``trace.get_tracer`` returns a proxy when no provider is
# installed, so importing this before ``setup_observability`` runs is safe —
# spans created via this tracer become no-ops until a provider is registered.
tracer = trace.get_tracer("parts_lookup")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Safe to call before ``setup_observability``."""
    return structlog.get_logger(name) if name else structlog.get_logger()


def capture_exception(exc: BaseException) -> None:
    """Send an exception to Sentry, no-op when Sentry isn't installed/configured.

    Used to page on-call for operator-fault upstream failures (e.g. exhausted
    Anthropic credit, bad API key). Safe to call unconditionally: if the
    ``sentry_sdk`` dependency is absent or ``setup_observability`` hasn't run,
    this is a silent no-op rather than a second exception on the error path.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    sentry_sdk.capture_exception(exc)


def setup_observability(settings: Settings) -> None:
    """Wire structlog, OpenTelemetry, and Sentry. Idempotent."""
    global _initialised
    if _initialised:
        return

    _configure_structlog(settings)
    _configure_otel(settings)
    _configure_sentry(settings)

    _initialised = True


def _configure_structlog(settings: Settings) -> None:
    log_level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(log_level, int):
        log_level = logging.INFO

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logs (uvicorn, sqlalchemy, httpx, etc.) through structlog so
    # everything in stdout is JSON.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    """Parse the OTel headers env format: 'Key=Value,Key2=Value2'.

    Values are URL-decoded per the OTel spec — this matters for headers that
    contain '=' or ',' (e.g. base64-encoded basic-auth tokens).
    """
    if not raw:
        return {}

    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            continue
        headers[key] = unquote(value.strip())
    return headers


def _configure_otel(settings: Settings) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        return

    # Imports are local so a missing optional dep doesn't break the rest of
    # observability setup.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.app_env,
        }
    )

    provider = TracerProvider(resource=resource)
    headers = _parse_otlp_headers(settings.otel_exporter_otlp_headers)
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint,
        headers=headers or None,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _instrument_optional("opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor")
    _instrument_optional("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")
    _instrument_optional(
        "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"
    )


def _instrument_optional(module_path: str, class_name: str) -> None:
    """Best-effort instrumentation: skip silently if the package isn't installed."""
    try:
        module = __import__(module_path, fromlist=[class_name])
        instrumentor_cls: Any = getattr(module, class_name)
        instrumentor_cls().instrument()
    except ImportError:
        return
    except Exception:
        # An already-instrumented library raises; treat that as a benign idempotency
        # signal rather than blowing up startup.
        return


def _configure_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
    except ImportError:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        traces_sample_rate=0.1,
        integrations=[FastApiIntegration()],
    )
