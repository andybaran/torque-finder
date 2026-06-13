"""HTTP request/response schemas.

These wrap the pure ``domain`` types in HTTP-shaped Pydantic models with
field metadata + examples so the OpenAPI doc at ``/docs`` is useful to the
future frontend developer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        ...,
        min_length=1,
        max_length=2_000,
        examples=["What torque should I use for the headset top cap bolt?"],
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=10,
        description=(
            "Document-selection breadth: how many top fused pages seed the "
            "candidate set. Default 5 (#29). NOTE (#30): this is no longer the "
            "total number of pages sent to Claude — the server expands the "
            "single leading document with neighbor pages and caps the total at "
            "a fixed page budget (retrieval_max_candidates). So the actual page "
            "count Claude sees can exceed top_k up to that budget."
        ),
    )


class Candidate(BaseModel):
    """A retrieval hit returned alongside the answer for transparency/debugging."""

    source_type: Literal["pdf", "html"]
    label: str = Field(..., examples=["p. 28 of mtb-manual.pdf", "Crank Arm Installation"])
    score: float
    source_url: str
    screenshot_url: str | None = Field(
        default=None, description="Rendered page PNG URL; null for HTML sources."
    )


class AnswerResponse(BaseModel):
    answer: str = Field(..., examples=["5 mm hex key, 11 N-m (97 in-lb)"])
    tool_size: str | None = Field(default=None, examples=["5 mm hex key"])
    torque: str | None = Field(default=None, examples=["11 N-m (97 in-lb)"])
    confidence: float = Field(..., ge=0.0, le=1.0)

    abstained: bool = Field(
        default=False,
        description=(
            "True when the API declined to answer because the queried product "
            "is not in the corpus (#32). On abstention source_type/source_url/"
            "screenshot_url are all null — there is no genuine source, so the "
            "frontend should render 'not in our manuals' distinctly from a "
            "low-confidence guess, NOT a (wrong-product) deep link."
        ),
    )

    source_type: Literal["pdf", "html"] | None = Field(
        default=None,
        description="Where the answer came from. Null on abstention.",
    )
    source_url: str | None = Field(
        default=None,
        description=(
            "Link to the source. Page-pinned when Claude cites a specific PDF "
            "page (the common case): the original PDF with a #page=N fragment, "
            "or the docs.sram.com publication with a #section hash. Doc-level "
            "best-effort otherwise (#49): the original PDF WITHOUT a #page "
            "fragment when no page can be honestly pinned — the document is "
            "always cited even when the exact page can't be. Null on abstention "
            "(no genuine source to link to)."
        ),
    )
    screenshot_url: str | None = Field(
        default=None,
        description=(
            "URL (public or presigned) to the rendered PNG of the source page. "
            "Present when a PDF page is pinned; null for HTML sources — the deep "
            "link is the source reference — null for a doc-level best-effort "
            "citation with no pinned page (#49), and null on abstention."
        ),
    )

    candidates: list[Candidate] = Field(
        default_factory=list,
        description=(
            "The top-k candidates considered, in fused-rank order. Carried even "
            "on abstention for transparency — these are the weak hits the "
            "retriever surfaced, not promoted to 'the answer'."
        ),
    )


class HealthResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    version: str


class ReadinessResponse(BaseModel):
    """Deep-readiness probe payload (/readyz).

    ``ready`` reflects whether the upstream answer service (Anthropic) was
    healthy on the most recent extraction call — distinct from ``/healthz``
    liveness, which is static. When ``ready`` is False the route returns 503.
    """

    ready: bool = Field(..., examples=[True])
    extraction_upstream_healthy: bool = Field(..., examples=[True])
    version: str
