"""HTTP request/response schemas.

These wrap the pure ``domain`` types in HTTP-shaped Pydantic models with
field metadata + examples so the OpenAPI doc at ``/docs`` is useful to the
future frontend developer.
"""

from __future__ import annotations

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


class CandidatePage(BaseModel):
    """A retrieval hit returned alongside the answer for transparency/debugging."""

    pdf_id: int
    pdf_filename: str
    page_no: int
    score: float
    png_url: str


class AnswerResponse(BaseModel):
    answer: str = Field(..., examples=["5 mm hex key, 11 N-m (97 in-lb)"])
    tool_size: str | None = Field(default=None, examples=["5 mm hex key"])
    torque: str | None = Field(default=None, examples=["11 N-m (97 in-lb)"])
    confidence: float = Field(..., ge=0.0, le=1.0)

    source_page_no: int
    source_page_png_url: str = Field(
        ...,
        description=(
            "URL (public or presigned) to the rendered PNG of the page the "
            "answer came from."
        ),
    )
    pdf_deep_link: str = Field(
        ...,
        description=(
            "URL to the original PDF, deep-linked to the source page via the "
            "#page= fragment."
        ),
    )

    candidates: list[CandidatePage] = Field(
        default_factory=list,
        description="The top-k candidates considered, in fused-rank order.",
    )


class HealthResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    version: str
