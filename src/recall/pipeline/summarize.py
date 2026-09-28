"""Meeting -> structured recap, via Claude structured outputs (JSON schema in recall.domain)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from recall import llm
from recall.config import get_settings
from recall.domain import RECAP_JSON_SCHEMA, RecapOut

log = logging.getLogger(__name__)

PROMPT_VERSION = "recap-v1"
MAP_REDUCE_THRESHOLD_TOKENS = 150_000
WINDOW_TOKENS = 40_000
MAX_OPEN_ITEMS = 30

SYSTEM_PROMPT = """\
You write meeting recaps for a team's shared memory. People act on these recaps — they chase action \
items, report status, and quote decisions — so accuracy matters more than completeness or polish.

Rules, with the reason for each:
- Use only facts stated in the transcript. Do not infer work that wasn't said; a wrong recap is worse than \
a short one.
- Never guess an owner or a due date. If the transcript doesn't make the owner or date clear, use null. \
A wrong owner sends someone chasing the wrong person.
- Resolve relative dates ("by Friday", "tomorrow", "end of next week") against the meeting date given \
above the transcript, and write them as YYYY-MM-DD.
- Keep each person's update in their own words, trimmed to the essentials, so they recognise it as theirs.
- Every topic, decision, action item and person update needs start_ms: the millisecond offset of the \
transcript line where it is discussed ([mm:ss] 04:12 = 252000). Users click these to verify.
- person_updates: one entry per speaker who gave a status update, with done / next / blockers. Required \
for standups; include them for other meetings only when people gave updates.
- completed_prior_action_item_ids: ids from the "Open action items" list that a speaker clearly says are \
finished (for example "I finished the migration"). Only use ids from that list.
- open_questions: questions raised and not answered in the meeting.
- kind: standup, planning, retro, sync or other.
- The overview is 3-6 plain sentences a teammate who missed the meeting can read in 20 seconds.
"""

INSTRUCTIONS = (
    "Write the recap for the meeting above as JSON matching the schema. Use the meeting date for relative "
    "dates, cite start_ms for every item, and leave owner/due null when they are not clear."
)


@dataclass
class MeetingContext:
    meeting_id: uuid.UUID
    team_id: uuid.UUID | None
    title: str
    kind: str
    started_at: dt.datetime
    duration_minutes: float
    participants: list[str]
    lines: list[str]  # "[mm:ss] Name: text"
    open_items: list[dict[str, str]] = field(default_factory=list)  # {"id", "description", "owner"}


def format_ms(ms: int) -> str:
    seconds = ms // 1000
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def choose_model(ctx: MeetingContext) -> tuple[str, str | None]:
    """(model, effort). Short standups go to Haiku (no effort parameter); everything else to Sonnet."""
    s = get_settings()
    if ctx.kind == "standup" and ctx.duration_minutes <= s.standup_haiku_max_minutes:
        return s.summarizer_standup_model, None
    return s.summarizer_model, s.summarizer_effort


def _user_content(ctx: MeetingContext, lines: list[str], extra: str = "") -> str:
    open_items = "\n".join(
        f"- id={i['id']} | {i['description']} | owner: {i.get('owner') or 'unassigned'}"
        for i in ctx.open_items[:MAX_OPEN_ITEMS]
    )
    header = (
        f"Meeting: {ctx.title}\nMeeting date: {ctx.started_at.date().isoformat()} "
        f"({ctx.started_at.strftime('%A')})\nKind (from calendar): {ctx.kind}\n"
        f"Participants: {', '.join(ctx.participants) or 'unknown'}\n"
    )
    transcript = "\n".join(lines)
    return (
        f"<transcript>\n{header}\n{transcript}\n</transcript>\n\n"
        f"<open_action_items>\n{open_items or '(none)'}\n</open_action_items>\n\n{INSTRUCTIONS}{extra}"
    )


def build_params(ctx: MeetingContext, lines: list[str] | None = None, extra: str = "") -> dict[str, Any]:
    model, effort = choose_model(ctx)
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": RECAP_JSON_SCHEMA}}
    if effort:
        output_config["effort"] = effort
    return {
        "model": model,
        "max_tokens": 16000,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": _user_content(ctx, lines or ctx.lines, extra)}],
        "output_config": output_config,
    }


def response_text(message: Any) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def parse_recap(raw: str) -> RecapOut:
    return RecapOut.model_validate(json.loads(raw))


async def _call(ctx: MeetingContext, lines: list[str], route: str) -> RecapOut:
    params = build_params(ctx, lines)
    message = await llm.create(route, team_id=ctx.team_id, meeting_id=ctx.meeting_id, **params)
    raw = response_text(message)
    try:
        return parse_recap(raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        log.info("recap for %s failed validation; retrying once", ctx.meeting_id)
        retry = build_params(ctx, lines, extra=f"\n\nYour previous answer failed validation: {exc}. Fix it.")
        message = await llm.create(route, team_id=ctx.team_id, meeting_id=ctx.meeting_id, **retry)
        return parse_recap(response_text(message))


async def summarize(ctx: MeetingContext) -> tuple[RecapOut, str]:
    """Real-time summarization. Returns (recap, model). Very long meetings are summarized in parallel windows
    and merged."""
    model, _ = choose_model(ctx)
    total = estimate_tokens("\n".join(ctx.lines))
    if total <= MAP_REDUCE_THRESHOLD_TOKENS:
        return await _call(ctx, ctx.lines, "summarize"), model
    windows: list[list[str]] = [[]]
    size = 0
    for line in ctx.lines:
        if size + estimate_tokens(line) > WINDOW_TOKENS and windows[-1]:
            windows.append([])
            size = 0
        windows[-1].append(line)
        size += estimate_tokens(line)
    partials = await asyncio.gather(*(_call(ctx, w, "summarize_window") for w in windows))
    merged_lines = [f"Partial recap {i + 1}: {p.model_dump_json()}" for i, p in enumerate(partials)]
    merge_ctx = MeetingContext(**{**ctx.__dict__, "lines": merged_lines})
    return await _call(merge_ctx, merged_lines, "summarize_merge"), model
