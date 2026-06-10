# tests/unit/test_ingest_cli.py
"""Argument-surface tests for the ingestion CLI (no I/O)."""

from __future__ import annotations

from parts_lookup.ingestion.cli import _build_parser


def test_ingest_html_no_args_means_all_pending() -> None:
    args = _build_parser().parse_args(["ingest-html"])
    assert args.command == "ingest-html"
    assert args.pub_ids == []


def test_ingest_html_accepts_pub_ids() -> None:
    args = _build_parser().parse_args(["ingest-html", "A1", "B2"])
    assert args.pub_ids == ["A1", "B2"]
