"""REST API (/v1), the Graph webhook and the Bot Framework endpoint."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sse_starlette.sse import EventSourceResponse

from recall.agent.service import ask, ask_stream
from recall.api.auth import AdminPrincipal, CurrentPrincipal
from recall.db.models import LlmUsage, Meeting, Person, Team, TeamMember
from recall.db.session import session_scope
from recall.domain import Answer
from recall.ingestion import graph
from recall.ingestion.storage import get_store
from recall.ingestion.vtt import parse_transcript_file
from recall.jobs import enqueue
from recall.pipeline.run import create_meeting
from recall.query.actions import get_action_items, set_status
from recall.query.auth import Forbidden, check_team
from recall.query.meetings import get_excerpt, get_header, get_recap, list_meetings
from recall.teams.bot import BotAuthError, get_connector, handle_activity

log = logging.getLogger(__name__)
router = APIRouter()

TRANSCRIPT_EXT = (".vtt", ".txt", ".docx")
MEDIA_EXT = (".mp3", ".m4a", ".wav", ".mp4", ".webm")


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    team_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------------------------------------


@router.get("/healthz")
async def healthz(deep: int = 0) -> dict[str, str]:
    if deep:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    return {"status": "ok"}


# ---------------------------------------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------------------------------------


@router.post("/v1/ask", response_model=None)
async def ask_endpoint(
    body: AskIn, principal: CurrentPrincipal, accept: Annotated[str | None, Header()] = None
) -> Answer | EventSourceResponse:
    """Streams SSE (`delta`, `reset`, `answer` events) when the client accepts text/event-stream; otherwise returns
    {answer, citations} as JSON."""
    try:
        check_team(principal, body.team_id)
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    if not accept or "text/event-stream" not in accept:
        return await ask(body.question, principal, body.team_id)

    async def events() -> Any:
        async for event in ask_stream(body.question, principal, body.team_id):
            if event.type == "answer" and event.answer is not None:
                yield {"event": "answer", "data": event.answer.model_dump_json(exclude={"card"})}
            else:
                yield {"event": event.type, "data": json.dumps({"text": event.text})}

    return EventSourceResponse(events())


# ---------------------------------------------------------------------------------------------------------
# Meetings and action items
# ---------------------------------------------------------------------------------------------------------


@router.get("/v1/meetings")
async def meetings_index(
    principal: CurrentPrincipal,
    team_id: uuid.UUID | None = None,
    kind: str | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: Annotated[int, Query(le=100)] = 20,
) -> list[dict[str, Any]]:
    try:
        async with session_scope() as session:
            rows = await list_meetings(
                session,
                principal,
                team_id=team_id,
                kind=kind,
                start=start,
                end=end,
                summarized_only=False,
                limit=limit,
            )
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    return [m.as_dict() for m in rows]


@router.get("/v1/meetings/{meeting_id}")
async def meeting_detail(meeting_id: uuid.UUID, principal: CurrentPrincipal) -> dict[str, Any]:
    async with session_scope() as session:
        recap = await get_recap(session, principal, meeting_id)
        if recap is None:
            header = await get_header(session, principal, meeting_id)
            if header is None:
                raise HTTPException(404, "meeting not found")
            meeting = await session.get(Meeting, meeting_id)
            assert meeting is not None
            return {**header.as_dict(), "error": meeting.error, "recap": None}
    header = recap.pop("meeting")
    return {**header.as_dict(), "recap": recap}


@router.get("/v1/meetings/{meeting_id}/excerpt")
async def meeting_excerpt(
    meeting_id: uuid.UUID, principal: CurrentPrincipal, start_ms: int = 0, end_ms: int | None = None
) -> dict[str, Any]:
    async with session_scope() as session:
        excerpt = await get_excerpt(session, principal, meeting_id, start_ms, end_ms or start_ms + 120_000)
    if excerpt is None:
        raise HTTPException(404, "meeting not found")
    return {**excerpt["meeting"].as_dict(), "segments": excerpt["segments"]}


@router.patch("/v1/meetings/{meeting_id}/speakers")
async def map_speakers(
    meeting_id: uuid.UUID, mapping: dict[str, uuid.UUID], principal: CurrentPrincipal
) -> dict[str, Any]:
    """Map diarized labels ("Speaker 1") to people, then re-run extraction (not speech-to-text)."""
    async with session_scope() as session:
        if await get_header(session, principal, meeting_id) is None:
            raise HTTPException(404, "meeting not found")
        meeting = await session.get(Meeting, meeting_id)
        assert meeting is not None
        meeting.speaker_overrides = {**meeting.speaker_overrides, **{k: str(v) for k, v in mapping.items()}}
        meeting.status, meeting.error, meeting.awaiting_batch = "queued", None, False
        await enqueue(session, "process_meeting", {"meeting_id": str(meeting_id), "realtime": True})
    return {"status": "queued"}


@router.get("/v1/action-items")
async def action_items(
    principal: CurrentPrincipal,
    team_id: uuid.UUID | None = None,
    status: Literal["open", "done", "dropped", "any"] = "open",
    mine: bool = False,
) -> list[dict[str, Any]]:
    try:
        async with session_scope() as session:
            items = await get_action_items(
                session,
                principal,
                status=None if status == "any" else status,
                owner_id=principal.person_id if mine else None,
                team_id=team_id,
                limit=100,
            )
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    return [
        {**i.as_dict(), "meeting": i.meeting.as_dict(), "citation": i.meeting.cite(i.cite_ms).label}
        for i in items
    ]


class ActionPatch(BaseModel):
    status: Literal["open", "done", "dropped"]


@router.patch("/v1/action-items/{item_id}")
async def patch_action_item(
    item_id: uuid.UUID, body: ActionPatch, principal: CurrentPrincipal
) -> dict[str, str]:
    async with session_scope() as session:
        item = await set_status(session, principal, item_id, body.status)
        if item is None:
            raise HTTPException(404, "action item not found")
        meeting = await session.get(Meeting, item.meeting_id)
        if meeting is not None and meeting.team_id is not None:
            from recall.pipeline.briefs import invalidate_answer_cache

            await invalidate_answer_cache(session, meeting.team_id)
    return {"id": str(item_id), "status": body.status}


# ---------------------------------------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------------------------------------


@router.post("/v1/upload", status_code=202)
async def upload(
    principal: CurrentPrincipal,
    file: Annotated[UploadFile, File()],
    team_id: Annotated[uuid.UUID, Form()],
    title: Annotated[str, Form()],
    started_at: Annotated[dt.datetime, Form()],
    kind: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    try:
        check_team(principal, team_id)
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    name = (file.filename or "upload").lower()
    data = await file.read()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=dt.UTC)
    cues, media_path = None, None
    if name.endswith(TRANSCRIPT_EXT):
        cues = parse_transcript_file(name, data)
        if not cues:
            raise HTTPException(422, "no transcript lines found in the file")
    elif name.endswith(MEDIA_EXT):
        media_path = await get_store().put(name, data)
    else:
        raise HTTPException(415, f"unsupported file type; use one of {TRANSCRIPT_EXT + MEDIA_EXT}")
    async with session_scope() as session:
        meeting = await create_meeting(
            session,
            title=title,
            source="upload",
            external_id=f"upload:{uuid.uuid4()}",
            team_id=team_id,
            started_at=started_at,
            kind=kind,
            cues=cues,
            media_path=media_path,
        )
        assert meeting is not None
    return {"meeting_id": str(meeting.id), "status": "queued"}


# ---------------------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------------------


@router.post("/graph/notifications")
async def graph_notifications(request: Request, validationToken: str | None = None) -> Response:
    if validationToken is not None:  # subscription handshake: echo the token as text/plain within 10 s
        return PlainTextResponse(validationToken)
    body = await request.json()
    await graph.handle_notifications(body)
    return Response(status_code=202)


@router.post("/api/messages")
async def bot_messages(
    request: Request, background: BackgroundTasks, authorization: Annotated[str | None, Header()] = None
) -> Response:
    activity = await request.json()
    try:
        await get_connector().verify(authorization, activity.get("serviceUrl"))
    except BotAuthError as exc:
        log.warning("rejected bot activity: %s", exc)
        return Response(status_code=401)
    # Reply after returning 200: Bot Framework expects a fast acknowledgement.
    background.add_task(_safe_handle, activity)
    return Response(status_code=200)


async def _safe_handle(activity: dict[str, Any]) -> None:
    try:
        await handle_activity(activity)
    except Exception:
        log.exception("bot activity failed")


# ---------------------------------------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------------------------------------


class TeamIn(BaseModel):
    name: str
    channel_id: str | None = None
    timezone: str = "UTC"
    standup_time: dt.time | None = None
    holidays: list[dt.date] = Field(default_factory=list)
    title_patterns: list[str] = Field(default_factory=list)
    join_urls: list[str] = Field(default_factory=list)
    monthly_budget_usd: Decimal | None = None
    retention_media_days: int | None = None
    retention_transcript_days: int | None = None
    retention_recap_days: int | None = None


class PersonIn(BaseModel):
    display_name: str
    email: str | None = None
    entra_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    is_admin: bool = False
    team_ids: list[uuid.UUID] = Field(default_factory=list)


def _team_dict(team: Team) -> dict[str, Any]:
    return {
        "id": str(team.id),
        "name": team.name,
        "channel_id": team.channel_id,
        "timezone": team.timezone,
        "standup_time": team.standup_time.isoformat() if team.standup_time else None,
        "monthly_budget_usd": str(team.monthly_budget_usd),
    }


@router.post("/v1/admin/teams", status_code=201)
async def create_team(body: TeamIn, _: AdminPrincipal) -> dict[str, Any]:
    from recall.config import get_settings

    values = body.model_dump(exclude_none=True)
    values.setdefault("monthly_budget_usd", get_settings().default_team_budget_usd)
    async with session_scope() as session:
        team = Team(**values)
        session.add(team)
        await session.flush()
        return _team_dict(team)


@router.patch("/v1/admin/teams/{team_id}")
async def update_team(team_id: uuid.UUID, body: TeamIn, _: AdminPrincipal) -> dict[str, Any]:
    async with session_scope() as session:
        team = await session.get(Team, team_id)
        if team is None:
            raise HTTPException(404, "team not found")
        for key, value in body.model_dump(exclude_unset=True).items():
            setattr(team, key, value)
        return _team_dict(team)


@router.post("/v1/admin/people", status_code=201)
async def upsert_person(body: PersonIn, _: AdminPrincipal) -> dict[str, Any]:
    async with session_scope() as session:
        person = None
        if body.email:
            person = await session.scalar(select(Person).where(Person.email == body.email.lower()))
        if person is None and body.entra_id:
            person = await session.scalar(select(Person).where(Person.entra_id == body.entra_id))
        if person is None:
            person = Person(display_name=body.display_name)
            session.add(person)
        person.display_name = body.display_name
        person.email = body.email.lower() if body.email else person.email
        person.entra_id = body.entra_id or person.entra_id
        person.aliases = body.aliases
        person.is_admin = body.is_admin
        await session.flush()
        for team_id in body.team_ids:
            await session.execute(
                insert(TeamMember).values(team_id=team_id, person_id=person.id).on_conflict_do_nothing()
            )
        return {"id": str(person.id), "display_name": person.display_name, "email": person.email}


@router.get("/v1/admin/usage")
async def usage(_: AdminPrincipal, days: int = 30) -> dict[str, Any]:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Team.name,
                    LlmUsage.route,
                    LlmUsage.model,
                    func.count(),
                    func.sum(LlmUsage.input_tokens),
                    func.sum(LlmUsage.cache_read_tokens),
                    func.sum(LlmUsage.output_tokens),
                    func.sum(LlmUsage.cost_usd),
                )
                .outerjoin(Team, Team.id == LlmUsage.team_id)
                .where(LlmUsage.ts >= since)
                .group_by(Team.name, LlmUsage.route, LlmUsage.model)
                .order_by(func.sum(LlmUsage.cost_usd).desc())
            )
        ).all()
    breakdown = [
        {
            "team": team or "(none)",
            "route": route,
            "model": model,
            "calls": calls,
            "input_tokens": int(inp or 0),
            "cache_read_tokens": int(cached or 0),
            "output_tokens": int(out or 0),
            "cost_usd": str(cost or 0),
        }
        for team, route, model, calls, inp, cached, out, cost in rows
    ]
    total = sum((Decimal(str(r["cost_usd"])) for r in breakdown), Decimal(0))
    return {"since": since.isoformat(), "total_usd": str(total), "breakdown": breakdown}


async def forbidden_handler(_: Request, exc: Forbidden) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=403)
