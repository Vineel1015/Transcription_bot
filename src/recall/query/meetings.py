"""Meeting reads. Every function takes a Principal and applies recall.query.auth filters."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import ActionItem, Meeting, Participant, Person, PersonUpdate, Recap, Segment, Team
from recall.domain import Citation
from recall.query.auth import Principal, meeting_scope
from recall.query.format import citation

MAX_EXCERPT_MS = 3 * 60 * 1000


@dataclass
class MeetingHeader:
    id: uuid.UUID
    team_id: uuid.UUID | None
    title: str
    kind: str
    started_at: dt.datetime
    ended_at: dt.datetime | None
    status: str
    timezone: str

    def cite(self, start_ms: int | None = 0) -> Citation:
        return citation(self.id, self.title, self.started_at, start_ms, self.timezone)

    def as_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": str(self.id),
            "title": self.title,
            "kind": self.kind,
            "started_at": self.started_at.isoformat(),
            "status": self.status,
        }


def header_query() -> Any:
    """SELECT of the eight MeetingHeader columns (meeting joined to its team's timezone)."""
    return select(
        Meeting.id,
        Meeting.team_id,
        Meeting.title,
        Meeting.kind,
        Meeting.started_at,
        Meeting.ended_at,
        Meeting.status,
        func.coalesce(Team.timezone, "UTC"),
    ).outerjoin(Team, Team.id == Meeting.team_id)


def to_headers(rows: Any) -> list[MeetingHeader]:
    return [MeetingHeader(*row) for row in rows]


async def list_meetings(
    session: AsyncSession,
    principal: Principal,
    *,
    team_id: uuid.UUID | None = None,
    kind: str | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    participant_id: uuid.UUID | None = None,
    summarized_only: bool = True,
    limit: int = 20,
) -> list[MeetingHeader]:
    stmt = header_query().where(meeting_scope(principal, team_id))
    if summarized_only:
        stmt = stmt.where(Meeting.status.in_(("summarized", "indexed")), Meeting.error.is_(None))
    if kind:
        stmt = stmt.where(Meeting.kind == kind)
    if start:
        stmt = stmt.where(Meeting.started_at >= start)
    if end:
        stmt = stmt.where(Meeting.started_at < end)
    if participant_id:
        attended = select(Participant.meeting_id).where(Participant.person_id == participant_id)
        spoke = select(Segment.meeting_id).where(Segment.person_id == participant_id)
        stmt = stmt.where(Meeting.id.in_(attended.union(spoke)))
    stmt = stmt.order_by(Meeting.started_at.desc()).limit(limit)
    return to_headers((await session.execute(stmt)).all())


async def get_header(
    session: AsyncSession, principal: Principal, meeting_id: uuid.UUID
) -> MeetingHeader | None:
    stmt = header_query().where(Meeting.id == meeting_id, meeting_scope(principal, None))
    rows = to_headers((await session.execute(stmt)).all())
    return rows[0] if rows else None


async def latest_meeting(
    session: AsyncSession,
    principal: Principal,
    *,
    team_id: uuid.UUID | None,
    kind: str | None = None,
    before: dt.datetime | None = None,
) -> MeetingHeader | None:
    found = await list_meetings(session, principal, team_id=team_id, kind=kind, end=before, limit=1)
    return found[0] if found else None


async def get_recap(
    session: AsyncSession, principal: Principal, meeting_id: uuid.UUID
) -> dict[str, Any] | None:
    """Full recap with action items and person updates, or None if missing or not visible."""
    header = await get_header(session, principal, meeting_id)
    if header is None:
        return None
    recap = await session.scalar(select(Recap).where(Recap.meeting_id == meeting_id))
    if recap is None:
        return None
    items = (
        await session.execute(
            select(ActionItem, Person.display_name)
            .outerjoin(Person, Person.id == ActionItem.owner_id)
            .where(ActionItem.meeting_id == meeting_id)
            .order_by(ActionItem.cite_ms)
        )
    ).all()
    updates = (
        await session.execute(
            select(PersonUpdate, Person.display_name)
            .outerjoin(Person, Person.id == PersonUpdate.person_id)
            .where(PersonUpdate.meeting_id == meeting_id)
            .order_by(PersonUpdate.cite_ms)
        )
    ).all()
    return {
        "meeting": header,
        "overview": recap.overview,
        "topics": recap.topics,
        "decisions": recap.decisions,
        "open_questions": recap.open_questions,
        "action_items": [
            {
                "id": str(item.id),
                "description": item.description,
                "owner": name or item.owner_text,
                "due": item.due_date.isoformat() if item.due_date else None,
                "status": item.status,
                "cite_ms": item.cite_ms,
            }
            for item, name in items
        ],
        "person_updates": [
            {
                "person_id": str(u.person_id) if u.person_id else None,
                "name": name or u.speaker_label,
                "done": u.done,
                "next": u.next,
                "blockers": u.blockers,
                "cite_ms": u.cite_ms,
            }
            for u, name in updates
        ],
    }


async def get_excerpt(
    session: AsyncSession, principal: Principal, meeting_id: uuid.UUID, start_ms: int, end_ms: int
) -> dict[str, Any] | None:
    header = await get_header(session, principal, meeting_id)
    if header is None:
        return None
    end_ms = min(end_ms, start_ms + MAX_EXCERPT_MS)
    rows = (
        await session.execute(
            select(Segment.speaker_label, Person.display_name, Segment.start_ms, Segment.text)
            .outerjoin(Person, Person.id == Segment.person_id)
            .where(
                Segment.meeting_id == meeting_id,
                and_(Segment.end_ms >= start_ms, Segment.start_ms <= end_ms),
            )
            .order_by(Segment.seq)
        )
    ).all()
    return {
        "meeting": header,
        "segments": [
            {"speaker": name or label, "start_ms": start, "text": text} for label, name, start, text in rows
        ],
    }


async def team_timezone(session: AsyncSession, team_id: uuid.UUID) -> tuple[str, list[dt.date]]:
    row = (await session.execute(select(Team.timezone, Team.holidays).where(Team.id == team_id))).first()
    return (row[0], list(row[1])) if row else ("UTC", [])
