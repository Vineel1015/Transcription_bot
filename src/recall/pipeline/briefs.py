"""Precomputed standup briefs — what makes the fast path fast."""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import AnswerCache, Brief, Team
from recall.query.briefs import brief_date_for, build_brief_content
from recall.teams.cards import brief_card


async def upsert_brief(session: AsyncSession, team: Team, for_date: dt.date) -> Brief:
    content = await build_brief_content(session, team, for_date)
    card = brief_card(content)
    stmt = (
        insert(Brief)
        .values(team_id=team.id, for_date=for_date, content=content, rendered_card=card)
        .on_conflict_do_update(
            constraint="uq_briefs_team_date", set_={"content": content, "rendered_card": card}
        )
        .returning(Brief)
    )
    brief: Brief = (await session.execute(stmt)).scalar_one()
    return brief


async def refresh_for_meeting(session: AsyncSession, team: Team, started_at: dt.datetime) -> Brief:
    meeting_day = started_at.astimezone(ZoneInfo(team.timezone)).date()
    return await upsert_brief(session, team, brief_date_for(meeting_day, list(team.holidays)))


async def invalidate_answer_cache(session: AsyncSession, team_id: uuid.UUID) -> None:
    await session.execute(delete(AnswerCache).where(AnswerCache.team_id == team_id))
