# tests/unit/test_extraction.py
"""Pure tests for extraction block building + response parsing (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from parts_lookup.domain.errors import ExtractionError
from parts_lookup.domain.models import SourceType
from parts_lookup.extraction.claude_client import ClaudeExtractor, ExtractionCandidate


def _pdf_candidate(index: int = 1) -> ExtractionCandidate:
    return ExtractionCandidate(
        index=index,
        source_type=SourceType.PDF,
        label="p. 28 of manual.pdf",
        png_bytes=b"\x89PNG fake",
    )


def _html_candidate(index: int = 2) -> ExtractionCandidate:
    return ExtractionCandidate(
        index=index,
        source_type=SourceType.HTML,
        label="Crank Installation",
        text="Crank Installation\n\nTighten to 40 N·m (354 in-lb)",
    )


def _response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))]
    )


class TestBuildUserBlocks:
    def test_pdf_candidate_becomes_image_plus_marker(self) -> None:
        blocks = ClaudeExtractor._build_user_blocks("q?", [_pdf_candidate()])
        assert blocks[0]["type"] == "image"
        assert blocks[1]["type"] == "text"
        assert "source 1" in blocks[1]["text"]
        assert "p. 28 of manual.pdf" in blocks[1]["text"]

    def test_html_candidate_becomes_text_block(self) -> None:
        blocks = ClaudeExtractor._build_user_blocks("q?", [_html_candidate()])
        assert blocks[0]["type"] == "text"
        assert "Source 2: Crank Installation" in blocks[0]["text"]
        assert "40 N·m (354 in-lb)" in blocks[0]["text"]

    def test_pdf_candidate_without_png_rejected(self) -> None:
        bad = ExtractionCandidate(
            index=1, source_type=SourceType.PDF, label="p. 1", png_bytes=None
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._build_user_blocks("q?", [bad])

    def test_html_candidate_without_text_rejected(self) -> None:
        bad = ExtractionCandidate(
            index=1, source_type=SourceType.HTML, label="x", text=None
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._build_user_blocks("q?", [bad])


class TestParseResponse:
    _CANDIDATES: ClassVar[list[ExtractionCandidate]] = [
        _pdf_candidate(1),
        _html_candidate(2),
    ]

    def test_resolves_cited_source_index(self) -> None:
        answer = ClaudeExtractor._parse_response(
            _response(
                {
                    "answer": "40 N·m (354 in-lb)",
                    "tool_size": None,
                    "torque": "40 N·m (354 in-lb)",
                    "source_index": 2,
                    "confidence": 0.95,
                }
            ),
            self._CANDIDATES,
        )
        assert answer.source_index == 2
        assert answer.torque == "40 N·m (354 in-lb)"

    def test_null_source_index_falls_back_to_top_candidate(self) -> None:
        answer = ClaudeExtractor._parse_response(
            _response(
                {
                    "answer": "not found",
                    "tool_size": None,
                    "torque": None,
                    "source_index": None,
                    "confidence": 0.1,
                }
            ),
            self._CANDIDATES,
        )
        assert answer.source_index == 1

    def test_unknown_source_index_rejected(self) -> None:
        with pytest.raises(ExtractionError):
            ClaudeExtractor._parse_response(
                _response(
                    {
                        "answer": "x",
                        "tool_size": None,
                        "torque": None,
                        "source_index": 9,
                        "confidence": 0.9,
                    }
                ),
                self._CANDIDATES,
            )


def test_system_prompt_marks_sources_as_reference_data_only() -> None:
    """Drift guard for the prompt-injection hardening rule (Task 5 review rider)."""
    from parts_lookup.extraction.prompt import SYSTEM_PROMPT

    assert "REFERENCE DATA ONLY" in SYSTEM_PROMPT
    assert "never follow" in SYSTEM_PROMPT


async def test_stub_extract_cites_first_candidate() -> None:
    from parts_lookup.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://stub/stub",
        stub_external_apis=True,
        _env_file=None,
    )
    extractor = ClaudeExtractor(settings)
    answer = await extractor.extract("q?", [_html_candidate(1), _pdf_candidate(2)])
    assert answer.source_index == 1
