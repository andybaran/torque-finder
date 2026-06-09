"""Voyage AI embeddings client.

Async wrapper around the official ``voyageai`` SDK. The SDK exposes an
``AsyncClient`` in v0.3+; we prefer it and fall back to running the sync
client in a thread if the async class is missing. Either way, callers see a
pure-async surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from typing import Any

import voyageai

from parts_lookup.config import Settings
from parts_lookup.domain.errors import RetrievalError

_QUERY_INPUT_TYPE = "query"
_DOCUMENT_INPUT_TYPE = "document"

_STUB_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")

# Voyage caps a single embed() request at ~1000 texts and ~120k tokens.
# We batch document embedding to stay safely under both. Token counts are
# estimated from character length (conservatively, ~3 chars/token) so we
# never need a tokenizer at ingest time; over-estimating just yields
# slightly smaller, safer batches.
_MAX_BATCH_TEXTS = 96
_MAX_BATCH_TOKENS = 100_000
_MAX_TEXT_CHARS = 96_000  # safety net: keep any single text under the per-input limit


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _stub_embed(text: str, dim: int) -> list[float]:
    """Hashing-trick bag-of-tokens embedding — deterministic and L2-normalised.

    Used only when ``Settings.stub_external_apis`` is true, so we can run
    end-to-end smoke tests without a Voyage key. Two inputs that share
    tokens still get non-zero cosine similarity, so vector retrieval has
    real signal.
    """
    counts = [0.0] * dim
    for token in _STUB_TOKEN_RE.findall(text.lower()):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = -1.0 if (digest[4] & 1) else 1.0
        counts[bucket] += sign
    norm = math.sqrt(sum(v * v for v in counts))
    if norm == 0.0:
        counts[0] = 1.0
        return counts
    return [v / norm for v in counts]


class VoyageEmbedder:
    """Async client for Voyage embeddings.

    One instance per process is enough; it holds the API key and model name.
    Use ``embed_query`` for the user's natural-language question and
    ``embed_documents`` for page text at ingest time — Voyage scores those
    asymmetrically when ``input_type`` is set.
    """

    def __init__(self, settings: Settings) -> None:
        self._model = settings.voyage_model
        self._dim = settings.voyage_dim
        self._stub = settings.stub_external_apis
        if self._stub:
            self._async_client: Any | None = None
            self._sync_client: Any | None = None
            return
        api_key = settings.voyage_api_key.get_secret_value()
        async_client_cls = getattr(voyageai, "AsyncClient", None)
        if async_client_cls is not None:
            self._async_client = async_client_cls(api_key=api_key)
            self._sync_client = None
        else:
            self._async_client = None
            self._sync_client = voyageai.Client(api_key=api_key)

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text], input_type=_QUERY_INPUT_TYPE)
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Split into Voyage-sized requests, bounded by both text count and an
        # estimated token budget, then concatenate in input order.
        vectors: list[list[float]] = []
        batch: list[str] = []
        batch_tokens = 0
        for text in texts:
            clipped = text[:_MAX_TEXT_CHARS]
            est = _estimate_tokens(clipped)
            if batch and (
                len(batch) >= _MAX_BATCH_TEXTS or batch_tokens + est > _MAX_BATCH_TOKENS
            ):
                vectors.extend(await self._embed(batch, input_type=_DOCUMENT_INPUT_TYPE))
                batch = []
                batch_tokens = 0
            batch.append(clipped)
            batch_tokens += est
        if batch:
            vectors.extend(await self._embed(batch, input_type=_DOCUMENT_INPUT_TYPE))
        return vectors

    async def _embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        if self._stub:
            return [_stub_embed(t, self._dim) for t in texts]
        try:
            if self._async_client is not None:
                result = await self._async_client.embed(
                    texts=texts,
                    model=self._model,
                    input_type=input_type,
                )
            else:
                assert self._sync_client is not None
                result = await asyncio.to_thread(
                    self._sync_client.embed,
                    texts=texts,
                    model=self._model,
                    input_type=input_type,
                )
        except Exception as exc:
            raise RetrievalError(
                f"Voyage embedding request failed for {len(texts)} input(s)"
            ) from exc

        embeddings = getattr(result, "embeddings", None)
        if embeddings is None or len(embeddings) != len(texts):
            raise RetrievalError(
                "Voyage embedding response was malformed (missing or short embeddings list)"
            )
        return [list(vec) for vec in embeddings]
