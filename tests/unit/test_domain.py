"""Pure unit tests for domain value objects. No I/O, no network."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from parts_lookup.domain.models import Answer, PdfDocument, Query


class TestQuery:
    def test_valid_minimum(self) -> None:
        q = Query(text="x")
        assert q.text == "x"
        assert q.top_k == 3

    def test_text_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Query(text="")

    def test_text_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Query(text="a" * 2_001)

    def test_top_k_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Query(text="hi", top_k=0)

    def test_top_k_too_large_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Query(text="hi", top_k=11)

    def test_top_k_bounds_inclusive(self) -> None:
        assert Query(text="hi", top_k=1).top_k == 1
        assert Query(text="hi", top_k=10).top_k == 10


class TestAnswer:
    @staticmethod
    def _kwargs(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "text": "Use a 5mm hex key.",
            "tool_size": "5mm hex",
            "torque": "11 N-m",
            "source_pdf_id": 1,
            "source_page_no": 31,
            "confidence": 0.9,
        }
        base.update(overrides)
        return base

    def test_valid(self) -> None:
        a = Answer(**self._kwargs())  # type: ignore[arg-type]
        assert a.confidence == 0.9

    def test_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Answer(**self._kwargs(confidence=-0.01))  # type: ignore[arg-type]

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Answer(**self._kwargs(confidence=1.01))  # type: ignore[arg-type]

    def test_confidence_bounds_inclusive(self) -> None:
        assert Answer(**self._kwargs(confidence=0.0)).confidence == 0.0  # type: ignore[arg-type]
        assert Answer(**self._kwargs(confidence=1.0)).confidence == 1.0  # type: ignore[arg-type]


class TestPdfDocument:
    @staticmethod
    def _kwargs(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": 1,
            "filename": "manual.pdf",
            "sha256": "a" * 64,
            "r2_key": "pdfs/manual.pdf",
            "page_count": 100,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        base.update(overrides)
        return base

    def test_valid(self) -> None:
        d = PdfDocument(**self._kwargs())  # type: ignore[arg-type]
        assert len(d.sha256) == 64

    def test_sha256_too_short_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PdfDocument(**self._kwargs(sha256="a" * 63))  # type: ignore[arg-type]

    def test_sha256_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PdfDocument(**self._kwargs(sha256="a" * 65))  # type: ignore[arg-type]

    def test_page_count_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            PdfDocument(**self._kwargs(page_count=0))  # type: ignore[arg-type]
