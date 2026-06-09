from __future__ import annotations


def test_publication_ref_is_frozen():
    from parts_lookup.domain.models import PublicationRef

    ref = PublicationRef(pub_id="abc", pub_type="UM", source_url="https://x/abc")
    assert ref.pub_id == "abc"
    import pytest

    with pytest.raises(Exception):
        ref.pub_id = "zzz"  # frozen


def test_discovered_publication_holds_filter_tuples():
    from parts_lookup.domain.models import DiscoveredPublication

    pub = DiscoveredPublication(
        pub_id="abc",
        pub_type="UM",
        title="Road AXS",
        locale="en-US",
        source_url="https://docs.sram.com/en-US/publications/abc",
        series=("red-axs",),
        models=("ed-red-e1",),
        procedures=("installation-axs",),
        content_hash="deadbeef",
    )
    assert pub.series == ("red-axs",)
    assert pub.models == ("ed-red-e1",)
