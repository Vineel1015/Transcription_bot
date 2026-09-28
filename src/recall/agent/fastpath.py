"""Answers that need no model call: served from briefs, person_updates, action_items and recaps."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from recall.domain import Answer, Citation
from recall.pipeline.speakers import PersonRef
from recall.query import dates
from recall.query.actions import get_action_items
from recall.query.auth import Principal
from recall.query.briefs import brief_date_for, build_brief_content, get_brief
from recall.query.meetings import get_recap, latest_meeting, list_meetings
from recall.query.people import get_person_updates
from recall.teams import cards


def _range(
    expr: str | None, default: str, tz: str, holidays: list[dt.date], now: dt.datetime | None
) -> dates.DateRange:
    rng = dates.resolve(expr, tz, holidays, now) if expr else None
    fallback = dates.resolve(default, tz, holidays, now)
    assert fallback is not None
    return rng or fallback


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


async def standup_brief(
    session: AsyncSession,
    team_id: uuid.UUID,
    tz: str,
    holidays: list[dt.date],
    expr: str | None,
    now: dt.datetime | None = None,
) -> Answer:
    covered = _range(expr, "yesterday", tz, holidays, now).first_day
    for_date = brief_date_for(covered, holidays)
    brief = await get_brief(session, team_id, for_date)
    if brief is not None:
        content, card = brief.content, brief.rendered_card
    else:
        from recall.db.models import Team

        team = await session.get(Team, team_id)
        assert team is not None
        content = await build_brief_content(session, team, for_date)
        card = cards.brief_card(content)
    citations = [Citation.model_validate(p["citation"]) for p in content["people"]]
    if not content["people"]:
        text = f"No standup updates were recorded for {content['covers_label']}."
    else:
        lines = []
        for i, p in enumerate(content["people"], 1):
            parts = []
            if p["done"]:
                parts.append("did " + "; ".join(p["done"]))
            if p["next"]:
                parts.append("next: " + "; ".join(p["next"]))
            if p["blockers"]:
                parts.append("blocked on " + "; ".join(p["blockers"]))
            lines.append(f"**{p['name']}** {' — '.join(parts) or 'no update'} [{i}]")
        text = f"Standup, {content['covers_label']}:\n" + _bullets(lines)
    return Answer(answer=text, citations=citations, intent="standup_brief", card=card)


async def person_recent(
    session: AsyncSession,
    principal: Principal,
    person: PersonRef,
    team_id: uuid.UUID | None,
    tz: str,
    holidays: list[dt.date],
    expr: str | None,
    now: dt.datetime | None = None,
) -> Answer:
    rng = dates.resolve(expr, tz, holidays, now) if expr else None
    rows = await get_person_updates(
        session,
        principal,
        person.id,
        start=rng.start if rng else None,
        end=rng.end if rng else None,
        team_id=team_id,
        limit=3,
    )
    if not rows:
        when = f" for {rng.label}" if rng else ""
        return Answer(
            answer=f"I have no recorded updates from {person.display_name}{when}.", intent="person_recent"
        )
    lines, citations = [], []
    for i, (meeting, upd) in enumerate(rows, 1):
        cite = meeting.cite(upd["cite_ms"])
        citations.append(cite)
        bits = []
        if upd["done"]:
            bits.append("did " + "; ".join(upd["done"]))
        if upd["next"]:
            bits.append("working on " + "; ".join(upd["next"]))
        if upd["blockers"]:
            bits.append("blocked on " + "; ".join(upd["blockers"]))
        lines.append(f"{cite.label.split(' · ')[1]}: {' — '.join(bits) or 'no details'} [{i}]")
    text = f"**{person.display_name}**\n" + _bullets(lines)
    return Answer(
        answer=text,
        citations=citations,
        intent="person_recent",
        card=cards.list_card(person.display_name, lines, citations),
    )


async def my_actions(session: AsyncSession, principal: Principal, team_id: uuid.UUID | None) -> Answer:
    if principal.person_id is None:
        return Answer(
            answer="I don't know who you are, so I can't list your action items.", intent="my_actions"
        )
    items = await get_action_items(session, principal, status="open", owner_id=principal.person_id, limit=20)
    if not items:
        return Answer(answer="You have no open action items.", intent="my_actions")
    lines, citations = [], []
    for i, item in enumerate(items, 1):
        cite = item.meeting.cite(item.cite_ms)
        citations.append(cite)
        due = f" (due {item.due_date:%a %d %b})" if item.due_date else ""
        lines.append(f"{item.description}{due} — {item.meeting.title} [{i}]")
    return Answer(
        answer=f"Your open action items ({len(items)}):\n" + _bullets(lines),
        citations=citations,
        intent="my_actions",
        card=cards.list_card("Your open action items", lines, citations),
    )


async def meeting_recap(
    session: AsyncSession,
    principal: Principal,
    team_id: uuid.UUID | None,
    tz: str,
    holidays: list[dt.date],
    expr: str | None,
    now: dt.datetime | None = None,
) -> Answer:
    rng = dates.resolve(expr, tz, holidays, now) if expr else None
    if rng:
        found = await list_meetings(
            session, principal, team_id=team_id, start=rng.start, end=rng.end, limit=1
        )
        meeting = found[0] if found else None
    else:
        meeting = await latest_meeting(session, principal, team_id=team_id)
    recap = await get_recap(session, principal, meeting.id) if meeting else None
    if meeting is None or recap is None:
        return Answer(answer="I couldn't find a summarized meeting for that.", intent="meeting_recap")
    cite = meeting.cite(0)
    lines = [recap["overview"]]
    if recap["decisions"]:
        lines.append("Decisions: " + "; ".join(d["decision"] for d in recap["decisions"]))
    if recap["action_items"]:
        lines.append(
            "Action items: "
            + "; ".join(f"{a['description']} ({a['owner'] or 'unassigned'})" for a in recap["action_items"])
        )
    text = f"**{meeting.title}** ({cite.label.split(' · ')[1]}) [1]\n" + _bullets(lines)
    return Answer(answer=text, citations=[cite], intent="meeting_recap", card=cards.recap_card(recap, cite))
