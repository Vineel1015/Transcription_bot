"""Hybrid search: pgvector cosine + Postgres full-text, merged with reciprocal rank fusion."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import Chunk, Meeting
from recall.pipeline.embed import get_embedder
from recall.query.auth import Principal, meeting_scope
from recall.query.meetings import MeetingHeader, header_query, to_headers

RRF_K = 60
CANDIDATES = 30
SNIPPET_CHARS = 300


@dataclass
class SearchHit:
    chunk_id: int
    score: float
    text: str
    start_ms: int | None
    meeting: MeetingHeader

    def snippet(self) -> str:
        text = " ".join(self.text.split())
        return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1] + "…"


def rrf_merge(*rankings: list[int], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion of ranked id lists -> [(id, score)] best first."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


async def hybrid_search(
    session: AsyncSession,
    principal: Principal,
    query: str,
    *,
    team_id: uuid.UUID | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    k: int = 5,
) -> list[SearchHit]:
    base_filters = [meeting_scope(principal, team_id)]
    if start:
        base_filters.append(Meeting.started_at >= start)
    if end:
        base_filters.append(Meeting.started_at < end)

    [vector] = await get_embedder().embed([query], query=True)
    vec_stmt = (
        select(Chunk.id)
        .join(Meeting, Meeting.id == Chunk.meeting_id)
        .where(*base_filters)
        .order_by(Chunk.embedding.cosine_distance(vector))
        .limit(CANDIDATES)
    )
    tsquery = func.websearch_to_tsquery("english", query)
    fts_stmt = (
        select(Chunk.id)
        .join(Meeting, Meeting.id == Chunk.meeting_id)
        .where(*base_filters, Chunk.tsv.op("@@")(tsquery))
        .order_by(func.ts_rank(Chunk.tsv, tsquery).desc())
        .limit(CANDIDATES)
    )
    vec_ids = list((await session.scalars(vec_stmt)).all())
    fts_ids = list((await session.scalars(fts_stmt)).all())
    merged = rrf_merge(vec_ids, fts_ids)[:k]
    if not merged:
        return []
    ids = [cid for cid, _ in merged]
    rows = (
        await session.execute(
            header_query()
            .add_columns(Chunk.id, Chunk.text, Chunk.start_ms)
            .join(Chunk, Chunk.meeting_id == Meeting.id)
            .where(Chunk.id.in_(ids))
        )
    ).all()
    by_id = {row[8]: row for row in rows}
    hits = []
    for cid, score in merged:
        row = by_id[cid]
        hits.append(SearchHit(cid, score, row[9], row[10], to_headers([row[:8]])[0]))
    return hits
