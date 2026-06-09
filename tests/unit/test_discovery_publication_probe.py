from __future__ import annotations

import hashlib
import json

import pytest

MANUAL_DATA = {
    "title": "Road AXS and XPLR AXS",
    "locale": "en-US",
    "filters": [
        {"key": "languages", "options": [{"value": "en-US", "text": "English"}]},
        {"key": "models", "options": [
            {"value": "ed-red-e1", "text": "ED-RED-E1"},
            {"value": "cn-red-e1", "text": "CN-RED-E1"},
        ]},
        {"key": "series", "options": [{"value": "red-axs", "text": "RED AXS"}]},
        {"key": "procedure", "options": [{"value": "installation-axs", "text": "Install"}]},
    ],
}
PUB_HTML = (
    "<html><head><title>x</title></head><body>"
    '<script id="manual-data" type="application/json">' + json.dumps(MANUAL_DATA) + "</script>"
    "</body></html>"
)


def test_extract_manual_data_json_roundtrips():
    from parts_lookup.discovery.publication_probe import extract_manual_data_json

    raw = extract_manual_data_json(PUB_HTML)
    assert json.loads(raw)["title"] == "Road AXS and XPLR AXS"


def test_extract_manual_data_json_missing_raises():
    from parts_lookup.discovery.publication_probe import extract_manual_data_json
    from parts_lookup.domain.errors import DiscoveryError

    with pytest.raises(DiscoveryError):
        extract_manual_data_json("<html>no script</html>")


def test_build_publication_pulls_filters_and_hash():
    from parts_lookup.discovery.publication_probe import build_publication
    from parts_lookup.domain.models import PublicationRef

    ref = PublicationRef(
        pub_id="6TmfV97fHWv8kvGXVegoTy",
        pub_type="UM",
        source_url="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy",
    )
    pub = build_publication(PUB_HTML, ref)

    assert pub.pub_id == "6TmfV97fHWv8kvGXVegoTy"
    assert pub.pub_type == "UM"
    assert pub.title == "Road AXS and XPLR AXS"
    assert pub.locale == "en-US"
    assert pub.series == ("red-axs",)
    assert pub.models == ("ed-red-e1", "cn-red-e1")
    assert pub.procedures == ("installation-axs",)
    raw = json.dumps(MANUAL_DATA)
    assert pub.content_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
