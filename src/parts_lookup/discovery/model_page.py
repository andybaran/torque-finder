"""Pure model-page HTML parser → publication references. No I/O."""

from __future__ import annotations

import re

from parts_lookup.domain.models import PublicationRef

# docs.sram.com/<locale>/publications/<pub_id>[/<TYPE>]
_PUB_RE = re.compile(
    r"https?://docs\.sram\.com/(?P<locale>[A-Za-z-]+)/publications/"
    r"(?P<pub_id>[A-Za-z0-9]+)(?:/(?P<type>[A-Z]{2}))?",
)


def parse_publication_refs(html: str) -> list[PublicationRef]:
    """Extract unique publication refs from a model page, preferring typed links.

    Order is stable: refs appear in first-seen order of their pub_id.
    """
    order: list[str] = []
    found: dict[str, PublicationRef] = {}

    for m in _PUB_RE.finditer(html):
        pub_id = m.group("pub_id")
        locale = m.group("locale")
        pub_type = m.group("type") or ""
        source_url = f"https://docs.sram.com/{locale}/publications/{pub_id}"
        ref = PublicationRef(pub_id=pub_id, pub_type=pub_type, source_url=source_url)

        if pub_id not in found:
            found[pub_id] = ref
            order.append(pub_id)
        elif pub_type and not found[pub_id].pub_type:
            # Upgrade a bare ref to a typed one.
            found[pub_id] = ref

    return [found[pid] for pid in order]
