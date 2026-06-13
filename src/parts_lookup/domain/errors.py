"""Domain-level exceptions. Each bounded context maps its failures into these."""

from __future__ import annotations


class PartsLookupError(Exception):
    """Base class. Catch this in the API layer to translate to HTTP errors."""


class IngestionError(PartsLookupError):
    """Ingestion pipeline failed (parse, render, embed, or store)."""


class RetrievalError(PartsLookupError):
    """Retrieval failed (embedding service, DB query, or fusion)."""


class ExtractionError(PartsLookupError):
    """Claude extraction failed or returned malformed output.

    This base class is the *parse/format* failure family (issue #25's
    territory): the model answered but the response was truncated
    (``stop_reason=max_tokens``), a refusal, non-JSON, or schema-mismatched.
    The API maps it to **502 Bad Gateway** — "Claude answered wrong."

    The two subclasses below are issue #33's territory — "Claude never
    answered" — i.e. the upstream call itself failed. They map to **503**.
    """


class ExtractionUpstreamUnavailable(ExtractionError):  # noqa: N818 (subclass of ExtractionError; the "Unavailable/Unauthorized" suffix is the ubiquitous-language term for the failure class)
    """The Anthropic call failed for a *transient* reason.

    Covers rate-limit (429), overload (529), connection/timeout (no HTTP
    response), and the 5xx fallback. Retryable; the API maps it to **503
    Service Unavailable** with a ``Retry-After`` header.

    ``status_code``/``request_id`` are ``APIStatusError``-only — the
    connection/timeout case carries neither, so both are ``int | None`` /
    ``str | None`` and must be read None-safely. ``retry_after`` is propagated
    from the upstream response header when present, else ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after


class ExtractionUpstreamUnauthorized(ExtractionError):  # noqa: N818 (subclass of ExtractionError; the "Unavailable/Unauthorized" suffix is the ubiquitous-language term for the failure class)
    """The Anthropic call failed for an *operator/code-fault* reason.

    Covers billing exhaustion (400 "credit balance too low"), auth (401),
    permission (403), and request-too-large (413). NOT transient — retrying
    will not help; an operator must intervene (top up credit, fix the key, or
    shrink the request). The API maps it to **503** with a generic
    operator-facing detail and captures it to Sentry so on-call is paged.

    All four cases are ``APIStatusError`` subclasses, so ``status_code`` and
    ``request_id`` are always present here (unlike the transient class).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class DiscoveryError(PartsLookupError):
    """Discovery/crawl failed (fetch, parse, or registry write)."""
