from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_model_page_fixture_yields_real_publication_refs():
    from parts_lookup.discovery.model_page import parse_publication_refs

    html = (FIXTURES / "sram_model_ed_red_e1.html").read_text(encoding="utf-8")
    refs = {r.pub_id: r for r in parse_publication_refs(html)}

    # The real ed-red-e1 page links these three publications.
    assert "6TmfV97fHWv8kvGXVegoTy" in refs  # User Manual
    assert refs["6TmfV97fHWv8kvGXVegoTy"].pub_type == "UM"
    assert "3AypAkC43AlFBouXgIFsDD" in refs  # Service Manual
    assert "2wamQedjkGP8QebD5HQiiC" in refs  # Bleed Manual


def test_publication_fixture_extracts_nonempty_filters():
    """Guards the real manual-data filter shape (options/value) — regression for
    the silent-empty-filters risk."""
    from parts_lookup.domain.models import PublicationRef
    from parts_lookup.discovery.publication_probe import build_publication

    html = (FIXTURES / "sram_publication_red_axs.html").read_text(encoding="utf-8")
    ref = PublicationRef(
        pub_id="6TmfV97fHWv8kvGXVegoTy",
        pub_type="UM",
        source_url="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy",
    )
    pub = build_publication(html, ref)

    assert pub.title == "Road AXS and XPLR AXS"
    assert pub.locale == "en-US"
    assert "red-axs" in pub.series
    assert "ed-red-e1" in pub.models
    assert len(pub.procedures) > 0
    assert len(pub.content_hash) == 64
