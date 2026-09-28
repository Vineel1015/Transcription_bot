from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx

from recall.db.models import ActionItem, Meeting
from recall.db.session import session_scope
from recall.ingestion.vtt import parse_vtt
from recall.jobs import drain
from recall.pipeline.run import create_meeting
from tests.conftest import FIXTURES

MONDAY = dt.datetime(2026, 9, 28, 14, 0, tzinfo=dt.UTC)  # 10:00 in New York
FRIDAY_STANDUP = dt.datetime(2026, 9, 25, 13, 30, tzinfo=dt.UTC)  # 09:30 in New York
DEV = {"Authorization": "Bearer dev"}


async def add_prior_open_item(team_id: uuid.UUID, owner_id: uuid.UUID) -> uuid.UUID:
    """Thursday's standup left 'Migrate billing to Postgres 16' open for Priya."""
    async with session_scope() as session:
        meeting = Meeting(
            team_id=team_id,
            title="Platform Standup",
            kind="standup",
            source="upload",
            external_id="thursday",
            started_at=FRIDAY_STANDUP - dt.timedelta(days=1),
            status="indexed",
        )
        session.add(meeting)
        await session.flush()
        item = ActionItem(
            meeting_id=meeting.id,
            description="Migrate the billing service to Postgres 16",
            owner_id=owner_id,
            owner_text="Priya Shah",
            cite_ms=0,
        )
        session.add(item)
        await session.flush()
        return item.id


async def ingest_fixture(
    team_id: uuid.UUID | None,
    name: str = "standup_2026-09-25.vtt",
    title: str = "Platform Standup",
    started_at: dt.datetime = FRIDAY_STANDUP,
    external_id: str | None = None,
    run: bool = True,
) -> uuid.UUID:
    cues = parse_vtt((FIXTURES / name).read_text())
    async with session_scope() as session:
        meeting = await create_meeting(
            session,
            title=title,
            source="upload",
            external_id=external_id or f"test:{uuid.uuid4()}",
            team_id=team_id,
            started_at=started_at,
            cues=cues,
        )
        assert meeting is not None
        meeting_id = meeting.id
    if run:
        await drain()
    return meeting_id


def api_client() -> httpx.AsyncClient:
    from recall.main import create_app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


async def get(model: Any, **filters: Any) -> list[Any]:
    from sqlalchemy import select

    async with session_scope() as session:
        stmt = select(model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(model, key) == value)
        return list((await session.scalars(stmt)).all())
