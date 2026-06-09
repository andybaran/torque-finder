"""Pure publication HTML → metadata. Extracts the embedded manual-data JSON. No I/O."""

from __future__ import annotations

import hashlib
import json
import re

from parts_lookup.domain.errors import DiscoveryError
from parts_lookup.domain.models import DiscoveredPublication, PublicationRef

_MANUAL_DATA_RE = re.compile(
    r'<script[^>]*id="manual-data"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_manual_data_json(html: str) -> str:
    """Return the raw JSON text of the <script id="manual-data"> block."""
    m = _MANUAL_DATA_RE.search(html)
    if not m:
        raise DiscoveryError("publication HTML has no <script id='manual-data'> block")
    return m.group(1).strip()


def _filter_values(data: dict, key: str) -> tuple[str, ...]:
    for f in data.get("filters", []):
        if f.get("key") == key:
            return tuple(o["value"] for o in f.get("options", []) if "value" in o)
    return ()


def build_publication(html: str, ref: PublicationRef) -> DiscoveredPublication:
    """Parse a publication page into a DiscoveredPublication metadata object."""
    raw = extract_manual_data_json(html)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"manual-data JSON for {ref.pub_id} is invalid") from exc

    return DiscoveredPublication(
        pub_id=ref.pub_id,
        pub_type=ref.pub_type,
        title=str(data.get("title", "")).strip(),
        locale=str(data.get("locale", "")),
        source_url=ref.source_url,
        series=_filter_values(data, "series"),
        models=_filter_values(data, "models"),
        procedures=_filter_values(data, "procedure"),
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )
