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
        default=3,
        ge=1,
        le=10,
        description="Number of candidate pages to ask Claude about.",
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

    source_type: Literal["pdf", "html"] = Field(
        ..., description="Where the answer came from."
    )
    source_url: str = Field(
        ...,
        description=(
            "Deep link to the source: the original PDF with a #page=N fragment, "
            "or the docs.sram.com publication with a #section hash."
        ),
    )
    screenshot_url: str | None = Field(
        default=None,
        description=(
            "URL (public or presigned) to the rendered PNG of the source page. "
            "Null for HTML sources — the deep link is the source reference."
        ),
    )

    candidates: list[Candidate] = Field(
        default_factory=list,
        description="The top-k candidates considered, in fused-rank order.",
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
