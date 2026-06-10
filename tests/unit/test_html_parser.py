# tests/unit/test_html_parser.py
"""Pure parser tests: synthetic manual-data (exact assertions) + captured
real fixture (structural assertions). No I/O beyond reading fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parts_lookup.domain.errors import IngestionError
from parts_lookup.ingestion.html_parser import parse_publication

_BASE = "https://docs.sram.com/en-US/publications/TESTPUB"
_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _wrap(data: dict) -> str:
    return (
        '<html><body><script id="manual-data" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script></body></html>"
    )


# Mirrors the real docs.sram.com manual-data shape (captured 2026-06-09):
# publication toolList is a dict whose ``content`` holds {label, description}
# items; block children carry a ``content`` list of {content, images} items;
# image captions live in caption1/caption2/caption3; tool lists also appear
# as children typed ``toolList``.
_SYNTHETIC = {
    "title": "Road AXS Test Manual ",
    "toolList": {
        "hash": "tools-and-supplies",
        "content": [
            {"label": "Hex", "description": "2, 2.5, 3, 4, 5, 8 mm", "type": "icon"},
            {"label": "TORX", "description": "T25", "type": "icon"},
        ],
    },
    "modules": [
        {
            "title": "Crank Arm Installation",
            "hash": "crank-install",
            "toolList": [{"label": "Hex", "description": "8 mm"}],
            "children": [
                {
                    "title": "Tighten the crank arm bolt",
                    "hash": "crank-bolt",
                    "type": "block",
                    "content": [
                        {
                            "type": "text",
                            "content": "<p>Grease the spindle threads.</p>",
                            "images": [
                                {
                                    "url": "x.png",
                                    "caption1": "8",
                                    "caption2": "40 N·m (354 in-lb)",
                                    "caption3": None,
                                }
                            ],
                        }
                    ],
                },
                {
                    # No hash on this block → anchor None, link falls back to module.
                    "content": "<p>Check that the arm spins freely.</p>",
                    "images": [],
                },
                {"content": "", "images": []},  # empty → skipped
            ],
        },
        {
            "title": "Tools and Supplies",
            "hash": "tools",
            "children": [
                {
                    # Real publications nest tool lists as a typed child.
                    "type": "toolList",
                    "hash": None,
                    "content": [
                        {"label": "Lockring Tool", "description": None, "type": "icon"}
                    ],
                },
            ],
        },
        {"title": "", "hash": None, "children": []},  # contributes nothing
    ],
}


class TestSyntheticManual:
    def setup_method(self) -> None:
        self.parsed = parse_publication(_wrap(_SYNTHETIC), base_url=_BASE)

    def test_title_stripped(self) -> None:
        assert self.parsed.title == "Road AXS Test Manual"

    def test_chunk_walk_order_and_ordinals(self) -> None:
        texts = [c.text for c in self.parsed.chunks]
        assert texts == [
            "Tools: Hex: 2, 2.5, 3, 4, 5, 8 mm; TORX: T25",
            "Crank Arm Installation",
            "Tools: Hex: 8 mm",
            "Tighten the crank arm bolt\nGrease the spindle threads.\n8\n40 N·m (354 in-lb)",
            "Check that the arm spins freely.",
            "Tools and Supplies",
            "Tools: Lockring Tool",
        ]
        assert [c.ordinal for c in self.parsed.chunks] == [1, 2, 3, 4, 5, 6, 7]

    def test_module_heading_chunk_anchors(self) -> None:
        heading = self.parsed.chunks[1]
        assert heading.anchor == "crank-install"
        assert heading.parent_anchor == "crank-install"
        assert heading.source_url == f"{_BASE}#crank-install"

    def test_block_with_hash_gets_own_anchor(self) -> None:
        block = self.parsed.chunks[3]
        assert block.anchor == "crank-bolt"
        assert block.parent_anchor == "crank-install"
        assert block.source_url == f"{_BASE}#crank-bolt"

    def test_block_without_hash_falls_back_to_module_anchor(self) -> None:
        block = self.parsed.chunks[4]
        assert block.anchor is None
        assert block.parent_anchor == "crank-install"
        assert block.source_url == f"{_BASE}#crank-install"

    def test_publication_toollist_has_no_anchor(self) -> None:
        tools = self.parsed.chunks[0]
        assert tools.anchor is None
        assert tools.parent_anchor is None
        assert tools.source_url == _BASE

    def test_toollist_child_becomes_tools_chunk(self) -> None:
        tools = self.parsed.chunks[6]
        assert tools.anchor is None
        assert tools.parent_anchor == "tools"
        assert tools.source_url == f"{_BASE}#tools"


def test_torque_caption_is_searchable_text() -> None:
    parsed = parse_publication(_wrap(_SYNTHETIC), base_url=_BASE)
    assert any("40 N·m (354 in-lb)" in c.text for c in parsed.chunks)


def test_missing_manual_data_raises_ingestion_error() -> None:
    with pytest.raises(IngestionError):
        parse_publication("<html><body>no script</body></html>", base_url=_BASE)


class TestCapturedFixture:
    """Structural invariants against the trimmed real Red AXS publication."""

    @pytest.fixture()
    def parsed(self):  # type: ignore[no-untyped-def]
        raw = (_FIXTURES / "sram_manual_data_red_axs_trimmed.json").read_text(
            encoding="utf-8"
        )
        return parse_publication(_wrap(json.loads(raw)), base_url=_BASE)

    def test_produces_chunks(self, parsed) -> None:  # type: ignore[no-untyped-def]
        assert len(parsed.chunks) >= 3

    def test_every_chunk_links_into_the_publication(self, parsed) -> None:  # type: ignore[no-untyped-def]
        assert all(c.source_url.startswith(_BASE) for c in parsed.chunks)

    def test_some_chunk_has_an_anchor(self, parsed) -> None:  # type: ignore[no-untyped-def]
        assert any(c.anchor for c in parsed.chunks)

    def test_torque_text_survives_if_present_in_fixture(self, parsed) -> None:  # type: ignore[no-untyped-def]
        # The committed fixture is known to carry torque captions.
        assert any("N·m" in c.text for c in parsed.chunks)

    def test_cassette_installation_xdr_chunk(self, parsed) -> None:  # type: ignore[no-untyped-def]
        chunk = next(
            (c for c in parsed.chunks if c.anchor == "cassette-installation-xdr"),
            None,
        )
        assert chunk is not None
        assert "40 N·m (354 In-lb)" in chunk.text
        assert chunk.parent_anchor == "cassette-installation"
