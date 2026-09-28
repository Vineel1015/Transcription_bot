"""The processing pipeline: normalize -> match speakers -> summarize -> persist -> embed -> precompute briefs.

Stages are idempotent and record meetings.status. Standups are summarized in real time; other meetings wait for
the Message Batches API (50% cheaper) unless USE_BATCH_API is off."""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recall import llm
from recall.config import get_settings
from recall.db.models import (
    ActionItem,
    Chunk,
    Meeting,
    Participant,
    PersonUpdate,
    Recap,
    Segment,
    Team,
)
from recall.db.session import session_scope
from recall.domain import Cue, RecapOut, SegmentIn
from recall.jobs import enqueue, handler, on_final_failure, periodic
from recall.pipeline import briefs, embed
from recall.pipeline.speakers import SpeakerMatcher
from recall.pipeline.summarize import (
    PROMPT_VERSION,
    MeetingContext,
    build_params,
    format_ms,
    parse_recap,
    response_text,
    summarize,
)
from recall.query.people import all_people, team_member_ids

log = logging.getLogger(__name__)

FILLER = {"um", "uh", "hmm", "mm", "mhm", "uh-huh", "er", "ah", "erm", "hm"}
MERGE_UNDER_MS = 2000
EMBED_BATCH = 64


# ---------------------------------------------------------------------------------------------------------
# Ingestion helpers
# ---------------------------------------------------------------------------------------------------------


def detect_kind(title: str) -> str:
    t = title.lower()
    if re.search(r"stand[\s-]?up|daily scrum|daily sync|\bdsu\b", t):
        return "standup"
    if re.search(r"planning|sprint plan|roadmap", t):
        return "planning"
    if re.search(r"retro", t):
        return "retro"
    if re.search(r"\bsync\b|1:1|one on one|check[\s-]?in", t):
        return "sync"
    return "other"


def normalize(cues: list[Cue]) -> list[SegmentIn]:
    """Drop filler-only cues, merge consecutive cues from the same speaker and fragments under 2 s."""
    segments: list[SegmentIn] = []
    for cue in cues:
        words = [w.strip(".,!?…").lower() for w in cue.text.split()]
        if not words or all(w in FILLER or not w for w in words):
            continue
        prev = segments[-1] if segments else None
        if prev is not None and prev.speaker_label == cue.speaker:
            prev.text = f"{prev.text} {cue.text}"
            prev.end_ms = max(prev.end_ms, cue.end_ms)
            continue
        if prev is not None and (cue.end_ms - cue.start_ms) < MERGE_UNDER_MS and len(words) <= 3:
            # a sub-2s interjection ("yep", "sure") from someone else: keep it inline, don't split the turn
            prev.text = f"{prev.text} [{cue.speaker}: {cue.text}]"
            continue
        segments.append(
            SegmentIn(
                seq=len(segments),
                speaker_label=cue.speaker,
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                text=cue.text,
            )
        )
    return segments


async def replace_segments(session: AsyncSession, meeting_id: uuid.UUID, segments: list[SegmentIn]) -> None:
    await session.execute(delete(Segment).where(Segment.meeting_id == meeting_id))
    session.add_all(
        Segment(
            meeting_id=meeting_id,
            seq=s.seq,
            speaker_label=s.speaker_label,
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            text=s.text,
        )
        for s in segments
    )


async def create_meeting(
    session: AsyncSession,
    *,
    title: str,
    source: str,
    started_at: dt.datetime,
    team_id: uuid.UUID | None = None,
    external_id: str | None = None,
    ended_at: dt.datetime | None = None,
    kind: str | None = None,
    cues: list[Cue] | None = None,
    media_path: str | None = None,
    graph_participants: list[dict[str, Any]] | None = None,
) -> Meeting | None:
    """Insert a meeting (deduplicated on source + external_id), store its transcript and queue processing.
    Returns None when the meeting already exists."""
    stmt = (
        insert(Meeting)
        .values(
            title=title,
            source=source,
            external_id=external_id,
            team_id=team_id,
            started_at=started_at,
            ended_at=ended_at,
            kind=kind or detect_kind(title),
            media_path=media_path,
            graph_participants=graph_participants or [],
        )
        .on_conflict_do_nothing(constraint="uq_meetings_source_external")
        .returning(Meeting)
    )
    meeting = (await session.execute(stmt)).scalar_one_or_none()
    if meeting is None:
        return None
    if cues:
        await replace_segments(session, meeting.id, normalize(cues))
    await enqueue(session, "process_meeting", {"meeting_id": str(meeting.id)})
    return meeting


# ---------------------------------------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------------------------------------


def skip_reason(segments: list[Segment], meeting: Meeting) -> str | None:
    s = get_settings()
    words = sum(len(seg.text.split()) for seg in segments)
    duration = (
        max((seg.end_ms for seg in segments), default=0) - min((seg.start_ms for seg in segments), default=0)
    ) / 1000
    if duration <= 0 and meeting.ended_at:
        duration = (meeting.ended_at - meeting.started_at).total_seconds()
    if not segments:
        return "skipped: empty transcript"
    if duration < s.min_meeting_seconds:
        return f"skipped: {int(duration)}s is under the {s.min_meeting_seconds}s minimum"
    if words < s.min_meeting_words:
        return f"skipped: {words} words is under the {s.min_meeting_words}-word minimum"
    return None


async def _load_segments(session: AsyncSession, meeting_id: uuid.UUID) -> list[Segment]:
    return list(
        (
            await session.scalars(
                select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.seq)
            )
        ).all()
    )


async def _matcher(session: AsyncSession, meeting: Meeting) -> SpeakerMatcher:
    local = await team_member_ids(session, meeting.team_id)
    local |= set(
        (
            await session.scalars(select(Participant.person_id).where(Participant.meeting_id == meeting.id))
        ).all()
    )
    return SpeakerMatcher(
        await all_people(session),
        participants=list(meeting.graph_participants),
        local_ids=local,
        overrides={k: str(v) for k, v in meeting.speaker_overrides.items()},
    )


async def match_speakers(session: AsyncSession, meeting: Meeting, segments: list[Segment]) -> SpeakerMatcher:
    matcher = await _matcher(session, meeting)
    for seg in segments:
        seg.person_id = matcher.match(seg.speaker_label)
    person_ids = {seg.person_id for seg in segments if seg.person_id}
    for part in meeting.graph_participants:
        pid = matcher.match(str(part.get("name") or part.get("email") or ""))
        if pid:
            person_ids.add(pid)
    if person_ids:
        await session.execute(
            insert(Participant)
            .values([{"meeting_id": meeting.id, "person_id": p} for p in person_ids])
            .on_conflict_do_nothing()
        )
    matcher.local_ids |= person_ids
    return matcher


async def build_context(session: AsyncSession, meeting: Meeting, segments: list[Segment]) -> MeetingContext:
    names = await _display_names(session, segments)
    lines = [
        f"[{format_ms(s.start_ms)}] {names.get(s.speaker_label, s.speaker_label)}: {s.text}" for s in segments
    ]
    open_items: list[dict[str, str]] = []
    if meeting.team_id:
        rows = (
            await session.execute(
                select(ActionItem.id, ActionItem.description, ActionItem.owner_text)
                .join(Meeting, Meeting.id == ActionItem.meeting_id)
                .where(
                    Meeting.team_id == meeting.team_id,
                    ActionItem.status == "open",
                    ActionItem.meeting_id != meeting.id,
                )
                .order_by(ActionItem.created_at.desc())
                .limit(30)
            )
        ).all()
        open_items = [{"id": str(i), "description": d, "owner": o or ""} for i, d, o in rows]
    duration = max((s.end_ms for s in segments), default=0) - min((s.start_ms for s in segments), default=0)
    return MeetingContext(
        meeting_id=meeting.id,
        team_id=meeting.team_id,
        title=meeting.title,
        kind=meeting.kind,
        started_at=meeting.started_at,
        duration_minutes=duration / 60_000,
        participants=sorted(set(names.values())),
        lines=lines,
        open_items=open_items,
    )


async def _display_names(session: AsyncSession, segments: list[Segment]) -> dict[str, str]:
    from recall.db.models import Person

    ids = {s.person_id for s in segments if s.person_id}
    people = {
        p.id: p.display_name for p in (await session.scalars(select(Person).where(Person.id.in_(ids)))).all()
    }
    return {
        s.speaker_label: people.get(s.person_id, s.speaker_label) if s.person_id else s.speaker_label
        for s in segments
    }


async def persist(meeting_id: uuid.UUID, recap: RecapOut, model: str) -> None:
    async with session_scope() as session:
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None:
            return
        matcher = await _matcher(session, meeting)
        await session.execute(delete(Recap).where(Recap.meeting_id == meeting_id))
        await session.execute(delete(ActionItem).where(ActionItem.meeting_id == meeting_id))
        await session.execute(delete(PersonUpdate).where(PersonUpdate.meeting_id == meeting_id))
        session.add(
            Recap(
                meeting_id=meeting_id,
                model=model,
                prompt_version=PROMPT_VERSION,
                overview=recap.overview,
                topics=[t.model_dump() for t in recap.topics],
                decisions=[d.model_dump() for d in recap.decisions],
                open_questions=recap.open_questions,
            )
        )
        for item in recap.action_items:
            session.add(
                ActionItem(
                    meeting_id=meeting_id,
                    description=item.description,
                    owner_id=matcher.match(item.owner) if item.owner else None,
                    owner_text=item.owner,
                    due_date=item.due,
                    cite_ms=item.start_ms,
                )
            )
        for upd in recap.person_updates:
            session.add(
                PersonUpdate(
                    meeting_id=meeting_id,
                    person_id=matcher.match(upd.speaker),
                    speaker_label=upd.speaker,
                    done=upd.done,
                    next=upd.next,
                    blockers=upd.blockers,
                    cite_ms=upd.start_ms,
                )
            )
        completed = [_uuid(i) for i in recap.completed_prior_action_item_ids]
        completed_ids = [i for i in completed if i is not None]
        if completed_ids and meeting.team_id:
            team_meetings = select(Meeting.id).where(Meeting.team_id == meeting.team_id)
            await session.execute(
                update(ActionItem)
                .where(
                    ActionItem.id.in_(completed_ids),
                    ActionItem.status == "open",
                    ActionItem.meeting_id.in_(team_meetings),
                    ActionItem.meeting_id != meeting_id,
                )
                .values(status="done", closed_in_meeting_id=meeting_id, updated_at=func.now())
            )
        if meeting.kind == "other" and recap.kind != "other":
            meeting.kind = recap.kind
        meeting.status = "summarized"
        meeting.error = None
        meeting.awaiting_batch = False


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def index(meeting_id: uuid.UUID, recap: RecapOut) -> None:
    async with session_scope() as session:
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None:
            return
        segments = await _load_segments(session, meeting_id)
        names = await _display_names(session, segments)
        seg_in = [
            SegmentIn(
                seq=s.seq, speaker_label=s.speaker_label, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text
            )
            for s in segments
        ]
        title = meeting.title
    chunks = embed.window_chunks(seg_in, names) + embed.recap_chunks(recap, title)
    embedder = embed.get_embedder()
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH):
        vectors.extend(await embedder.embed([c.text for c in chunks[start : start + EMBED_BATCH]]))
    async with session_scope() as session:
        await session.execute(delete(Chunk).where(Chunk.meeting_id == meeting_id))
        session.add_all(
            Chunk(
                meeting_id=meeting_id,
                kind=c.kind,
                text=c.text,
                start_ms=c.start_ms,
                end_ms=c.end_ms,
                embedding=v,
            )
            for c, v in zip(chunks, vectors, strict=True)
        )
        meeting = await session.get(Meeting, meeting_id)
        assert meeting is not None
        meeting.status = "indexed"
        if meeting.team_id:
            team = await session.get(Team, meeting.team_id)
            assert team is not None
            await session.flush()
            await briefs.refresh_for_meeting(session, team, meeting.started_at)
            await briefs.invalidate_answer_cache(session, team.id)


async def finish(meeting_id: uuid.UUID, recap: RecapOut, model: str) -> None:
    await persist(meeting_id, recap, model)
    await index(meeting_id, recap)


# ---------------------------------------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------------------------------------


@handler("process_meeting")
async def process_meeting(payload: dict[str, Any]) -> None:
    meeting_id = uuid.UUID(payload["meeting_id"])
    realtime = bool(payload.get("realtime"))
    async with session_scope() as session:
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None:
            return
        segments = await _load_segments(session, meeting_id)
        if not segments and meeting.media_path:
            from recall.ingestion.storage import get_store
            from recall.ingestion.stt import transcribe

            data = await get_store().get(meeting.media_path)
            await replace_segments(session, meeting_id, normalize(await transcribe(data, meeting.media_path)))
            await session.flush()
            segments = await _load_segments(session, meeting_id)
        reason = skip_reason(segments, meeting)
        if reason:
            meeting.status, meeting.error = "indexed", reason
            log.info("meeting %s %s", meeting_id, reason)
            return
        await match_speakers(session, meeting, segments)
        if meeting.kind != "standup" and get_settings().use_batch_api and not realtime:
            meeting.awaiting_batch, meeting.batch_id = True, None
            return
        ctx = await build_context(session, meeting, segments)
    recap, model = await summarize(ctx)
    await finish(meeting_id, recap, model)


@on_final_failure("process_meeting")
async def mark_failed(payload: dict[str, Any], error: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(Meeting)
            .where(Meeting.id == uuid.UUID(payload["meeting_id"]))
            .values(status="failed", error=error, awaiting_batch=False)
        )


@periodic("batch_submit", dt.timedelta(minutes=10))
async def batch_submit() -> None:
    """Send meetings waiting for batch summarization as one Message Batch (custom_id = meeting id)."""
    from anthropic.types.messages.batch_create_params import Request

    requests: list[Request] = []
    ids: list[uuid.UUID] = []
    async with session_scope() as session:
        meetings = (
            await session.scalars(
                select(Meeting).where(Meeting.awaiting_batch.is_(True), Meeting.batch_id.is_(None)).limit(500)
            )
        ).all()
        for meeting in meetings:
            ctx = await build_context(session, meeting, await _load_segments(session, meeting.id))
            requests.append(Request(custom_id=str(meeting.id), params=build_params(ctx)))  # type: ignore[typeddict-item]
            ids.append(meeting.id)
    if not requests:
        return
    batch = await llm.get_client().messages.batches.create(requests=requests)
    async with session_scope() as session:
        await session.execute(update(Meeting).where(Meeting.id.in_(ids)).values(batch_id=batch.id))
    log.info("submitted batch %s with %d meetings", batch.id, len(ids))


@periodic("batch_collect", dt.timedelta(minutes=5))
async def batch_collect() -> None:
    async with session_scope() as session:
        batch_ids = (
            await session.scalars(
                select(Meeting.batch_id)
                .where(Meeting.awaiting_batch.is_(True), Meeting.batch_id.is_not(None))
                .distinct()
            )
        ).all()
    client = llm.get_client()
    for batch_id in batch_ids:
        assert batch_id is not None
        batch = await client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            continue
        async for result in await client.messages.batches.results(batch_id):
            await _collect_one(uuid.UUID(result.custom_id), result)


async def _collect_one(meeting_id: uuid.UUID, result: Any) -> None:
    async with session_scope() as session:
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None or not meeting.awaiting_batch:
            return
        team_id = meeting.team_id
    if result.result.type == "succeeded":
        message = result.result.message
        await llm.record_usage(
            "summarize", message.model, message.usage, team_id=team_id, meeting_id=meeting_id, batch=True
        )
        try:
            recap = parse_recap(response_text(message))
        except Exception:
            log.warning("batch recap for %s failed validation; retrying in real time", meeting_id)
        else:
            await finish(meeting_id, recap, message.model)
            return
    # errored, expired, canceled or invalid: fall back to a real-time summary
    async with session_scope() as session:
        await session.execute(
            update(Meeting).where(Meeting.id == meeting_id).values(awaiting_batch=False, batch_id=None)
        )
        await enqueue(session, "process_meeting", {"meeting_id": str(meeting_id), "realtime": True})
