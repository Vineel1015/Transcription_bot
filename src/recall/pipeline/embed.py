"""Embedding providers and chunking.

Providers share one interface. `local` (fastembed, bge-small, 384 dims) needs no key; `voyage` needs
VOYAGE_API_KEY and an EMBEDDING_DIM Voyage supports (512 or 1024) set before the first migration; `hash` is a
deterministic bag-of-words embedding for tests and offline benchmarks."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from recall.config import get_settings
from recall.domain import RecapOut, SegmentIn

WINDOW_WORDS = 225  # ~300 tokens
OVERLAP_WORDS = 34  # ~15%


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic hashed bag of words (unigrams + bigrams), L2-normalised."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        words = re.findall(r"[a-z0-9]+", text.lower())
        grams = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
        for gram in grams:
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class LocalEmbedder:
    def __init__(self, model_name: str, dim: int) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        def run() -> list[list[float]]:
            model = self._load()
            method = model.query_embed if query else model.embed
            return [[float(x) for x in vec] for vec in method(texts)]

        return await asyncio.to_thread(run)


class VoyageEmbedder:
    URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str, dim: int) -> None:
        self.api_key = api_key
        self.model = model
        self.dim = dim

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as client:
            for start in range(0, len(texts), 128):
                resp = await client.post(
                    self.URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "input": texts[start : start + 128],
                        "model": self.model,
                        "input_type": "query" if query else "document",
                        "output_dimension": self.dim,
                    },
                )
                resp.raise_for_status()
                out.extend(
                    item["embedding"] for item in sorted(resp.json()["data"], key=lambda d: d["index"])
                )
        return out


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        s = get_settings()
        if s.embeddings_provider == "voyage":
            _embedder = VoyageEmbedder(s.voyage_api_key, s.voyage_model, s.embedding_dim)
        elif s.embeddings_provider == "hash":
            _embedder = HashEmbedder(s.embedding_dim)
        else:
            _embedder = LocalEmbedder(s.local_embedding_model, s.embedding_dim)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


@dataclass
class ChunkIn:
    kind: str
    text: str
    start_ms: int | None
    end_ms: int | None


def window_chunks(segments: list[SegmentIn], names: dict[str, str]) -> list[ChunkIn]:
    """~300-token sliding windows over the transcript with speaker names inlined and ~15% overlap."""
    words: list[tuple[str, int, int]] = []  # (token, start_ms, end_ms)
    for seg in segments:
        name = names.get(seg.speaker_label, seg.speaker_label)
        tokens = seg.text.split()
        words.append((f"\n{name}:", seg.start_ms, seg.end_ms))
        words.extend((tok, seg.start_ms, seg.end_ms) for tok in tokens)
    chunks: list[ChunkIn] = []
    step = WINDOW_WORDS - OVERLAP_WORDS
    for start in range(0, len(words), step):
        window = words[start : start + WINDOW_WORDS]
        if not window:
            break
        text = " ".join(tok for tok, _, _ in window).replace(" \n", "\n").strip()
        chunks.append(ChunkIn("segment_window", text, window[0][1], window[-1][2]))
        if start + WINDOW_WORDS >= len(words):
            break
    return chunks


def recap_chunks(recap: RecapOut, title: str) -> list[ChunkIn]:
    out = [ChunkIn("recap_section", f"{title} — overview: {recap.overview}", 0, None)]
    for t in recap.topics:
        out.append(ChunkIn("recap_section", f"{title} — topic {t.title}: {t.summary}", t.start_ms, None))
    for d in recap.decisions:
        text = f"{title} — decision: {d.decision}. Rationale: {d.rationale}"
        out.append(ChunkIn("recap_section", text, d.start_ms, None))
    for a in recap.action_items:
        owner = a.owner or "unassigned"
        due = f", due {a.due.isoformat()}" if a.due else ""
        out.append(
            ChunkIn(
                "recap_section", f"{title} — action item ({owner}{due}): {a.description}", a.start_ms, None
            )
        )
    for u in recap.person_updates:
        parts = [
            f"done: {'; '.join(u.done)}" if u.done else "",
            f"next: {'; '.join(u.next)}" if u.next else "",
        ]
        if u.blockers:
            parts.append(f"blockers: {'; '.join(u.blockers)}")
        body = ". ".join(p for p in parts if p)
        out.append(ChunkIn("recap_section", f"{title} — {u.speaker} update: {body}", u.start_ms, None))
    return out
