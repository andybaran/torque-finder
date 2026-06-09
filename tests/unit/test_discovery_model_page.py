from __future__ import annotations

MODEL_HTML = """
<html><body>
<a href="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy/UM">User Manual</a>
<a href="https://docs.sram.com/en-US/publications/3AypAkC43AlFBouXgIFsDD/SM">Service Manual</a>
<a href="https://docs.sram.com/en-US/publications/2wamQedjkGP8QebD5HQiiC/BM">Bleed</a>
<a href="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy">dup, no type</a>
<a href="https://www.sram.com/en/other">unrelated</a>
</body></html>
"""


def test_parse_publication_refs_dedups_and_keeps_types():
    from parts_lookup.discovery.model_page import parse_publication_refs

    refs = parse_publication_refs(MODEL_HTML)
    by_id = {r.pub_id: r for r in refs}

    assert set(by_id) == {
        "6TmfV97fHWv8kvGXVegoTy",
        "3AypAkC43AlFBouXgIFsDD",
        "2wamQedjkGP8QebD5HQiiC",
    }
    # the typed variant wins over the bare duplicate
    assert by_id["6TmfV97fHWv8kvGXVegoTy"].pub_type == "UM"
    assert by_id["6TmfV97fHWv8kvGXVegoTy"].source_url == (
        "https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy"
    )
    assert by_id["3AypAkC43AlFBouXgIFsDD"].pub_type == "SM"


def test_parse_publication_refs_empty_when_none():
    from parts_lookup.discovery.model_page import parse_publication_refs

    assert parse_publication_refs("<html>nothing here</html>") == []
