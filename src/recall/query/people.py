from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import Meeting, Person, PersonUpdate, TeamMember
from recall.pipeline.speakers import PersonRef, SpeakerMatcher
from recall.query.auth import Principal, meeting_scope
from recall.query.meetings import MeetingHeader, header_query, to_headers


async def all_people(session: AsyncSession) -> list[PersonRef]:
    rows = (await session.scalars(select(Person))).all()
    return [PersonRef(p.id, p.display_name, p.email, p.entra_id, tuple(p.aliases)) for p in rows]


async def team_member_ids(session: AsyncSession, team_id: uuid.UUID | None) -> set[uuid.UUID]:
    if team_id is None:
        return set()
    return set(
        (await session.scalars(select(TeamMember.person_id).where(TeamMember.team_id == team_id))).all()
    )


async def find_person(
    session: AsyncSession, name_or_email: str, team_id: uuid.UUID | None = None
) -> PersonRef | None:
    people = await all_people(session)
    matcher = SpeakerMatcher(people, local_ids=await team_member_ids(session, team_id))
    person_id = matcher.match(name_or_email)
    return next((p for p in people if p.id == person_id), None)


async def get_person_updates(
    session: AsyncSession,
    principal: Principal,
    person_id: uuid.UUID,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    team_id: uuid.UUID | None = None,
    limit: int = 10,
) -> list[tuple[MeetingHeader, dict[str, Any]]]:
    stmt = (
        header_query()
        .add_columns(PersonUpdate.done, PersonUpdate.next, PersonUpdate.blockers, PersonUpdate.cite_ms)
        .join(PersonUpdate, PersonUpdate.meeting_id == Meeting.id)
        .where(PersonUpdate.person_id == person_id, meeting_scope(principal, team_id))
    )
    if start:
        stmt = stmt.where(Meeting.started_at >= start)
    if end:
        stmt = stmt.where(Meeting.started_at < end)
    rows = (await session.execute(stmt.order_by(Meeting.started_at.desc()).limit(limit))).all()
    out = []
    for row in rows:
        header = to_headers([row[:8]])[0]
        done, nxt, blockers, cite_ms = row[8:]
        out.append((header, {"done": done, "next": nxt, "blockers": blockers, "cite_ms": cite_ms}))
    return out
