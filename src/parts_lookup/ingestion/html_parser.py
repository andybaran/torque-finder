# src/parts_lookup/ingestion/html_parser.py
"""Pure parser: SRAM publication HTML → ParsedPublication. No network, no DB.

The publication page embeds the whole manual as JSON in
``<script id="manual-data">`` (Contentful-backed; investigated 2026-06-08,
shape re-verified against the live Red AXS publication 2026-06-09):
``modules`` → ``children`` (blocks) → ``content`` is a list of items whose
``content`` key holds HTML ``<p>`` text and whose ``images`` carry torque
values in ``caption1``/``caption2``/``caption3``; the publication-level
``toolList`` is a dict whose ``content`` items give tool sizes as
``label``/``description``; tool lists also appear as children typed
``toolList``; every module/child has a ``hash`` used for section deep links.

Chunking (spec §2.3, §4): one chunk per block, plus a heading chunk per module
(text = module title, anchor = parent_anchor = module hash) and a chunk per
toolList. The heading chunk means module reconstruction
(``parent_anchor = ?`` ordered by ordinal) always starts with the title —
which the API uses as the candidate label.
"""

from __future__ import annotations

import html as html_module
import json
import re

from parts_lookup.discovery.publication_probe import extract_manual_data_json
from parts_lookup.domain.errors import DiscoveryError, IngestionError
from parts_lookup.domain.models import HtmlChunk, ParsedPublication

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(fragment: str) -> str:
    """HTML fragment → plain text: drop tags, unescape entities, collapse spaces."""
    text = _TAG_RE.sub(" ", fragment)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _hash_of(node: dict) -> str | None:
    value = str(node.get("hash") or "").strip().lstrip("#")
    return value or None


def _deep_link(base_url: str, anchor: str | None) -> str:
    return f"{base_url}#{anchor}" if anchor else base_url


def _image_captions(images: object) -> list[str]:
    captions: list[str] = []
    if not isinstance(images, list):
        return captions
    for image in images:
        if not isinstance(image, dict):
            continue
        for key in ("caption1", "caption2", "caption3"):
            value = str(image.get(key) or "").strip()
            if value:
                captions.append(value)
    return captions


def _content_texts(content: object) -> list[str]:
    """Flatten a block's ``content`` into text fragments.

    Tolerates a bare HTML string, a list of strings, or (lists of) dicts
    carrying ``content``/``html``/``text``/``value`` plus nested ``images``.
    """
    parts: list[str] = []
    if content is None:
        return parts
    if isinstance(content, str):
        text = _strip_html(content)
        if text:
            parts.append(text)
        return parts
    if isinstance(content, dict):
        for key in ("content", "html", "text", "value"):
            value = content.get(key)
            if isinstance(value, str):
                text = _strip_html(value)
                if text:
                    parts.append(text)
                break
        parts.extend(_image_captions(content.get("images")))
        return parts
    if isinstance(content, list):
        for item in content:
            parts.extend(_content_texts(item))
    return parts


def _tool_list_text(tool_list: object) -> str:
    """toolList → one searchable line, e.g. 'Tools: Hex: 2, 2.5 mm; TORX: T25'."""
    if not tool_list:
        return ""
    if isinstance(tool_list, dict):
        tool_list = tool_list.get("content")
    entries: list[str] = []
    if isinstance(tool_list, list):
        for item in tool_list:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                label = str(item.get("label") or item.get("title") or "").strip()
                value = item.get("description") or item.get("value") or ""
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                value = str(value).strip()
                text = f"{label}: {value}".strip(": ").strip()
            else:
                text = ""
            if text:
                entries.append(text)
    if not entries:
        return ""
    return "Tools: " + "; ".join(entries)


def _block_text(child: dict) -> str:
    """Block title + paragraph text + image captions + tools, de-duplicated."""
    parts: list[str] = []
    title = str(child.get("title") or "").strip()
    if title:
        parts.append(title)
    if str(child.get("type") or "") == "toolList":
        # Tool lists nest as a typed child whose ``content`` holds the tool
        # items (label/description), not paragraph blocks.
        tool_text = _tool_list_text(child)
    else:
        parts.extend(_content_texts(child.get("content")))
        parts.extend(_image_captions(child.get("images")))
        tool_text = _tool_list_text(child.get("toolList"))
    if tool_text:
        parts.append(tool_text)
    seen: set[str] = set()
    unique = [p for p in parts if not (p in seen or seen.add(p))]
    return "\n".join(unique).strip()


def parse_publication(html: str, base_url: str) -> ParsedPublication:
    """Parse one publication page into title + ordered block-level chunks."""
    try:
        raw = extract_manual_data_json(html)
    except DiscoveryError as exc:
        raise IngestionError(str(exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IngestionError("manual-data JSON is invalid") from exc

    title = str(data.get("title") or "").strip()
    chunks: list[HtmlChunk] = []
    ordinal = 0

    def _append(text: str, anchor: str | None, parent_anchor: str | None) -> None:
        nonlocal ordinal
        text = text.strip()
        if not text:
            return
        ordinal += 1
        chunks.append(
            HtmlChunk(
                ordinal=ordinal,
                text=text,
                anchor=anchor,
                parent_anchor=parent_anchor,
                source_url=_deep_link(base_url, anchor or parent_anchor),
            )
        )

    _append(_tool_list_text(data.get("toolList")), None, None)

    for module in data.get("modules") or []:
        if not isinstance(module, dict):
            continue
        module_hash = _hash_of(module)
        _append(str(module.get("title") or "").strip(), module_hash, module_hash)
        _append(_tool_list_text(module.get("toolList")), None, module_hash)
        for child in module.get("children") or []:
            if not isinstance(child, dict):
                continue
            _append(_block_text(child), _hash_of(child), module_hash)

    return ParsedPublication(title=title, chunks=tuple(chunks))
