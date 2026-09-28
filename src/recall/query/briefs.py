"""Standup brief content: the previous working day's meetings for a team, per-person updates, action items
due or overdue, and unresolved blockers. Pure data — rendering lives in recall.teams.cards."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import Brief, Team
from recall.query import dates
from recall.query.actions import get_action_items
from recall.query.auth import Principal
from recall.query.meetings import get_recap, list_meetings


def covered_day(for_date: dt.date, holidays: list[dt.date]) -> dt.date:
    return dates.previous_working_day(for_date, holidays)


def brief_date_for(covered: dt.date, holidays: list[dt.date]) -> dt.date:
    """The standup day whose brief covers `covered`."""
    return dates.next_working_day(covered, holidays)


async def build_brief_content(session: AsyncSession, team: Team, for_date: dt.date) -> dict[str, Any]:
    holidays = list(team.holidays)
    covers = covered_day(for_date, holidays)
    rng = dates.day_range(covers, covers, team.timezone, covers.strftime("%a %d %b"))
    principal = Principal.service()
    meetings = await list_meetings(
        session, principal, team_id=team.id, start=rng.start, end=rng.end, limit=20
    )
    meetings.sort(key=lambda m: m.started_at)
    people: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for meeting in meetings:
        recap = await get_recap(session, principal, meeting.id)
        if recap is None:
            continue
        for update in recap["person_updates"]:
            cite = meeting.cite(update["cite_ms"]).model_dump(mode="json")
            people.append({**update, "meeting_id": str(meeting.id), "citation": cite})
            blockers.extend(
                {"name": update["name"], "blocker": b, "citation": cite} for b in update["blockers"]
            )
    due_items = []
    for item in await get_action_items(session, principal, team_id=team.id, status="open", limit=50):
        if item.due_date is not None and item.due_date <= for_date:
            due_items.append(
                {
                    **item.as_dict(),
                    "overdue": item.due_date < for_date,
                    "citation": item.meeting.cite(item.cite_ms).model_dump(mode="json"),
                }
            )
    return {
        "team": team.name,
        "team_id": str(team.id),
        "for_date": for_date.isoformat(),
        "covers_date": covers.isoformat(),
        "covers_label": rng.label,
        "meetings": [{**m.as_dict(), "citation": m.cite(0).model_dump(mode="json")} for m in meetings],
        "people": people,
        "due_items": due_items,
        "blockers": blockers,
    }


async def get_brief(session: AsyncSession, team_id: uuid.UUID, for_date: dt.date) -> Brief | None:
    return await session.scalar(select(Brief).where(Brief.team_id == team_id, Brief.for_date == for_date))
