"""ask(): the one entry point for questions from REST, the Teams bot and scripts.

Order: answer cache -> router -> fast path (no model call) -> budget check -> Retrieval Agent (escalating effort
once if nothing was found) -> cache the answer. Every question is written to audit_log."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recall import llm
from recall.agent import fastpath
from recall.agent.agent import run_agent
from recall.agent.router import Route, route
from recall.agent.tools import ToolContext
from recall.config import get_settings
from recall.db.models import AnswerCache, AuditLog, Meeting, Participant
from recall.db.session import session_scope
from recall.domain import Answer, Citation
from recall.query import dates
from recall.query.auth import Principal, check_team
from recall.query.meetings import team_timezone
from recall.query.people import all_people
from recall.query.search import hybrid_search
from recall.teams import cards


@dataclass
class StreamEvent:
    type: str  # "delta" | "reset" | "answer"
    text: str = ""
    answer: Answer | None = None


def normalize_question(question: str) -> str:
    q = re.sub(r"\s+", " ", question.lower()).strip()
    return q.rstrip("?.! ")


def cache_key(question: str, tz: str, holidays: list[dt.date], now: dt.datetime | None) -> tuple[str, str]:
    """(question_hash, date_scope). Date expressions resolve to concrete days, so "yesterday" asked on two different
    days never shares an entry; questions without dates share one until the team's next meeting is indexed."""
    normalized = normalize_question(question)
    expr = dates.find_date_expression(normalized)
    rng = dates.resolve(expr, tz, holidays, now) if expr else None
    return hashlib.sha256(normalized.encode()).hexdigest(), rng.scope_key if rng else ""


async def default_team(session: AsyncSession, principal: Principal) -> uuid.UUID | None:
    """The asker's team when none is given: their only team, else the team of their most recent meeting."""
    if len(principal.team_ids) == 1:
        return next(iter(principal.team_ids))
    if principal.person_id is None or not principal.team_ids:
        return None
    team_id: uuid.UUID | None = await session.scalar(
        select(Meeting.team_id)
        .join(Participant, Participant.meeting_id == Meeting.id)
        .where(Participant.person_id == principal.person_id, Meeting.team_id.in_(principal.team_ids))
        .order_by(Meeting.started_at.desc())
        .limit(1)
    )
    return team_id or sorted(principal.team_ids)[0]


async def ask(
    question: str, principal: Principal, team_id: uuid.UUID | None = None, now: dt.datetime | None = None
) -> Answer:
    answer: Answer | None = None
    async for event in ask_stream(question, principal, team_id, now):
        if event.type == "answer":
            answer = event.answer
    assert answer is not None
    return answer


async def ask_stream(
    question: str, principal: Principal, team_id: uuid.UUID | None = None, now: dt.datetime | None = None
) -> AsyncIterator[StreamEvent]:
    async with session_scope() as session:
        check_team(principal, team_id)
        team_id = team_id or await default_team(session, principal)
        tz, holidays = await team_timezone(session, team_id) if team_id else ("UTC", [])
        qhash, scope = cache_key(question, tz, holidays, now)

        answer: Answer | None = None
        if team_id is not None:
            cached = await session.scalar(
                select(AnswerCache).where(
                    AnswerCache.team_id == team_id,
                    AnswerCache.question_hash == qhash,
                    AnswerCache.date_scope == scope,
                )
            )
            if cached is not None:
                citations = [Citation.model_validate(c) for c in cached.citations]
                answer = Answer(
                    answer=cached.answer, citations=citations, intent="open_question", cached=True
                )
                answer.card = cards.answer_card(answer)

        over = team_id is not None and await llm.over_budget(team_id)
        if answer is None:
            people = await all_people(session)
            routed = await route(question, people, principal.person_id, team_id, allow_llm=not over)
            answer = await _fast_path(session, routed, principal, team_id, tz, holidays, now)

        if answer is None and over and team_id is not None:
            answer = await _search_only(session, question, principal, team_id)

        if answer is None:
            ctx = ToolContext(
                session=session, principal=principal, team_id=team_id, timezone=tz, holidays=holidays, now=now
            )
            s = get_settings()
            async for event in run_agent(question, ctx):
                if event.type == "done":
                    answer = Answer(
                        answer=event.text, citations=event.citations or [], intent="open_question"
                    )
                else:
                    yield StreamEvent(event.type, event.text)
            if answer is not None and not ctx.found_anything and s.agent_escalation_effort != s.agent_effort:
                yield StreamEvent("reset")
                async for event in run_agent(question, ctx, effort=s.agent_escalation_effort):
                    if event.type == "done":
                        answer = Answer(
                            answer=event.text, citations=event.citations or [], intent="open_question"
                        )
                    else:
                        yield StreamEvent(event.type, event.text)
            assert answer is not None
            answer.card = cards.answer_card(answer)
            if team_id is not None and ctx.found_anything:
                await session.execute(
                    insert(AnswerCache)
                    .values(
                        team_id=team_id,
                        question_hash=qhash,
                        date_scope=scope,
                        answer=answer.answer,
                        citations=[c.model_dump(mode="json") for c in answer.citations],
                    )
                    .on_conflict_do_nothing(constraint="uq_answer_cache_key")
                )
            session.add(
                AuditLog(
                    actor_id=principal.person_id,
                    action="agent_fallthrough",
                    target=str(team_id) if team_id else None,
                    detail={"question": question},
                )
            )

        session.add(
            AuditLog(
                actor_id=principal.person_id,
                action="ask",
                target=str(team_id) if team_id else None,
                detail={"question": question, "intent": answer.intent, "cached": answer.cached},
            )
        )
    yield StreamEvent("answer", answer=answer)


async def _fast_path(
    session: AsyncSession,
    routed: Route,
    principal: Principal,
    team_id: uuid.UUID | None,
    tz: str,
    holidays: list[dt.date],
    now: dt.datetime | None,
) -> Answer | None:
    if routed.intent == "standup_brief" and team_id is not None:
        return await fastpath.standup_brief(session, team_id, tz, holidays, routed.date_expression, now)
    if routed.intent == "person_recent" and routed.person is not None:
        return await fastpath.person_recent(
            session, principal, routed.person, team_id, tz, holidays, routed.date_expression, now
        )
    if routed.intent == "my_actions":
        return await fastpath.my_actions(session, principal, team_id)
    if routed.intent == "meeting_recap":
        return await fastpath.meeting_recap(
            session, principal, team_id, tz, holidays, routed.date_expression, now
        )
    return None


async def _search_only(
    session: AsyncSession, question: str, principal: Principal, team_id: uuid.UUID
) -> Answer:
    """Over budget: no model call, just the most relevant passages."""
    hits = await hybrid_search(session, principal, question, team_id=team_id, k=5)
    if not hits:
        text = "Your team has reached this month's AI budget, and search found nothing relevant."
        return Answer(answer=text, intent="search_only")
    citations = [h.meeting.cite(h.start_ms) for h in hits]
    lines = [f"- {h.snippet()} [{i}]" for i, h in enumerate(hits, 1)]
    text = (
        "Your team has reached this month's AI budget, so here are the most relevant passages:\n"
        + "\n".join(lines)
    )
    answer = Answer(answer=text, citations=citations, intent="search_only")
    answer.card = cards.answer_card(answer)
    return answer
