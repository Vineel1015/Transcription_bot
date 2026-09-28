"""Retrieval Agent tools: JSON schemas plus thin wrappers over recall.query.

Tool results are compact JSON. Every citable item carries a `ref` like "m2@252000" (meeting alias @ ms);
the agent cites with [m2@252000] and the service turns those into numbered citations."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from recall.domain import Citation
from recall.query import dates
from recall.query.actions import get_action_items
from recall.query.auth import Principal
from recall.query.meetings import MeetingHeader, get_excerpt, get_header, get_recap, list_meetings
from recall.query.people import find_person, get_person_updates
from recall.query.search import hybrid_search

REF = re.compile(r"\[(m\d+)@(\d+)\]")

_DATE = {"type": "string", "description": "YYYY-MM-DD (inclusive), in the team's timezone"}
_REF = {"type": "string", "description": "meeting ref from an earlier tool result, e.g. m2"}


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


# Order and content are fixed so the cached prompt prefix is byte-identical on every call.
TOOLS: list[dict[str, Any]] = [
    _tool(
        "resolve_dates",
        "Turn a date expression ('yesterday', 'last week', 'Tuesday', '2026-09-25') into a "
        "start/end date range. 'Yesterday' means the previous working day.",
        {"expression": {"type": "string"}},
        ["expression"],
    ),
    _tool(
        "list_meetings",
        "List summarized meetings, newest first. Filter by kind (standup, planning, retro, sync, "
        "other), date range, and participant name.",
        {"kind": {"type": "string"}, "start": _DATE, "end": _DATE, "participant": {"type": "string"}},
        [],
    ),
    _tool(
        "get_recap",
        "Full recap of one meeting: overview, topics, decisions, action items, per-person updates, "
        "open questions. Prefer this over transcript excerpts.",
        {"meeting": _REF},
        ["meeting"],
    ),
    _tool(
        "get_person_updates",
        "A person's done/next/blockers updates from recent meetings.",
        {"person": {"type": "string", "description": "name or email"}, "start": _DATE, "end": _DATE},
        ["person"],
    ),
    _tool(
        "get_action_items",
        "Action items, filtered by status (open, done, dropped), owner name, and meetings since a date.",
        {"status": {"type": "string"}, "owner": {"type": "string"}, "since": _DATE},
        [],
    ),
    _tool(
        "search",
        "Hybrid keyword + semantic search over transcripts and recaps. Returns short snippets.",
        {"query": {"type": "string"}, "start": _DATE, "end": _DATE, "k": {"type": "integer"}},
        ["query"],
    ),
    _tool(
        "get_excerpt",
        "Raw transcript lines from one meeting between start_ms and end_ms (at most 3 minutes). "
        "Use only when a recap or snippet is not enough.",
        {"meeting": _REF, "start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}},
        ["meeting", "start_ms", "end_ms"],
    ),
]


@dataclass
class ToolContext:
    session: AsyncSession
    principal: Principal
    team_id: uuid.UUID | None
    timezone: str = "UTC"
    holidays: list[dt.date] = field(default_factory=list)
    now: dt.datetime | None = None
    _aliases: dict[uuid.UUID, str] = field(default_factory=dict)
    _meetings: dict[str, MeetingHeader] = field(default_factory=dict)
    found_anything: bool = False

    def ref(self, meeting: MeetingHeader, ms: int | None = 0) -> str:
        alias = self._aliases.get(meeting.id)
        if alias is None:
            alias = f"m{len(self._aliases) + 1}"
            self._aliases[meeting.id] = alias
            self._meetings[alias] = meeting
        return f"{alias}@{ms or 0}"

    def meeting_for(self, ref: str) -> MeetingHeader | None:
        return self._meetings.get(ref.split("@")[0].strip())

    def citation(self, alias: str, ms: int) -> Citation | None:
        meeting = self._meetings.get(alias)
        return meeting.cite(ms) if meeting else None

    def day_bounds(self, start: str | None, end: str | None) -> tuple[dt.datetime | None, dt.datetime | None]:
        lo = hi = None
        if start:
            d = dt.date.fromisoformat(start)
            lo = dates.day_range(d, d, self.timezone, "").start
        if end:
            d = dt.date.fromisoformat(end)
            hi = dates.day_range(d, d, self.timezone, "").end
        return lo, hi


def _meeting_brief(ctx: ToolContext, m: MeetingHeader) -> dict[str, Any]:
    return {
        "ref": ctx.ref(m).split("@")[0],
        "title": m.title,
        "kind": m.kind,
        "date": m.cite().label.split(" · ")[1],
    }


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    try:
        result = await _dispatch(ctx, name, args)
    except (ValueError, KeyError) as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


async def _dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> Any:
    s, p = ctx.session, ctx.principal
    if name == "resolve_dates":
        rng = dates.resolve(args["expression"], ctx.timezone, ctx.holidays, ctx.now)
        if rng is None:
            return {"error": "unrecognised date expression"}
        return {"start": rng.first_day.isoformat(), "end": rng.last_day.isoformat(), "label": rng.label}

    if name == "list_meetings":
        start, end = ctx.day_bounds(args.get("start"), args.get("end"))
        participant = None
        if args.get("participant"):
            ref = await find_person(s, args["participant"], ctx.team_id)
            if ref is None:
                return {"error": f"no person matching {args['participant']!r}"}
            participant = ref.id
        rows = await list_meetings(
            s,
            p,
            team_id=ctx.team_id,
            kind=args.get("kind"),
            start=start,
            end=end,
            participant_id=participant,
            limit=15,
        )
        ctx.found_anything |= bool(rows)
        return {"meetings": [_meeting_brief(ctx, m) for m in rows]}

    if name == "get_recap":
        meeting = await _resolve_meeting(ctx, args["meeting"])
        recap = await get_recap(s, p, meeting.id) if meeting else None
        if recap is None:
            return {"error": "meeting not found or not summarized"}
        ctx.found_anything = True
        m = recap["meeting"]
        return {
            **_meeting_brief(ctx, m),
            "overview": recap["overview"],
            "topics": [
                {"title": t["title"], "summary": t["summary"], "ref": ctx.ref(m, t.get("start_ms"))}
                for t in recap["topics"]
            ],
            "decisions": [
                {
                    "decision": d["decision"],
                    "rationale": d.get("rationale", ""),
                    "ref": ctx.ref(m, d.get("start_ms")),
                }
                for d in recap["decisions"]
            ],
            "action_items": [
                {
                    "description": a["description"],
                    "owner": a["owner"],
                    "due": a["due"],
                    "status": a["status"],
                    "ref": ctx.ref(m, a["cite_ms"]),
                }
                for a in recap["action_items"]
            ],
            "person_updates": [
                {
                    "name": u["name"],
                    "done": u["done"],
                    "next": u["next"],
                    "blockers": u["blockers"],
                    "ref": ctx.ref(m, u["cite_ms"]),
                }
                for u in recap["person_updates"]
            ],
            "open_questions": recap["open_questions"],
        }

    if name == "get_person_updates":
        person = await find_person(s, args["person"], ctx.team_id)
        if person is None:
            return {"error": f"no person matching {args['person']!r}"}
        start, end = ctx.day_bounds(args.get("start"), args.get("end"))
        updates = await get_person_updates(
            s, p, person.id, start=start, end=end, team_id=ctx.team_id, limit=8
        )
        ctx.found_anything |= bool(updates)
        return {
            "person": person.display_name,
            "updates": [
                {**_meeting_brief(ctx, mh), **upd, "ref": ctx.ref(mh, upd.pop("cite_ms"))}
                for mh, upd in updates
            ],
        }

    if name == "get_action_items":
        owner_id = None
        if args.get("owner"):
            ref = await find_person(s, args["owner"], ctx.team_id)
            if ref is None:
                return {"error": f"no person matching {args['owner']!r}"}
            owner_id = ref.id
        since, _ = ctx.day_bounds(args.get("since"), None)
        status = args.get("status") or "open"
        items = await get_action_items(
            s,
            p,
            status=None if status == "any" else status,
            owner_id=owner_id,
            team_id=ctx.team_id,
            since=since,
            limit=20,
        )
        ctx.found_anything |= bool(items)
        return {
            "action_items": [
                {**i.as_dict(), "meeting": i.meeting.title, "ref": ctx.ref(i.meeting, i.cite_ms)}
                for i in items
            ]
        }

    if name == "search":
        start, end = ctx.day_bounds(args.get("start"), args.get("end"))
        k = max(1, min(int(args.get("k") or 5), 8))
        hits = await hybrid_search(s, p, args["query"], team_id=ctx.team_id, start=start, end=end, k=k)
        ctx.found_anything |= bool(hits)
        return {
            "results": [
                {
                    **_meeting_brief(ctx, h.meeting),
                    "snippet": h.snippet(),
                    "ref": ctx.ref(h.meeting, h.start_ms),
                }
                for h in hits
            ]
        }

    if name == "get_excerpt":
        meeting = await _resolve_meeting(ctx, args["meeting"])
        excerpt = (
            await get_excerpt(s, p, meeting.id, int(args["start_ms"]), int(args["end_ms"]))
            if meeting
            else None
        )
        if excerpt is None:
            return {"error": "meeting not found"}
        m = excerpt["meeting"]
        return {
            **_meeting_brief(ctx, m),
            "lines": [
                {"speaker": x["speaker"], "text": x["text"], "ref": ctx.ref(m, x["start_ms"])}
                for x in excerpt["segments"]
            ],
        }

    return {"error": f"unknown tool {name}"}


async def _resolve_meeting(ctx: ToolContext, ref: str) -> MeetingHeader | None:
    known = ctx.meeting_for(ref)
    if known is not None:
        return known
    try:
        header = await get_header(ctx.session, ctx.principal, uuid.UUID(ref))
    except ValueError:
        return None
    # A team-scoped question only sees that team's meetings (this is what makes the answer cache safe to share).
    if header is not None and ctx.team_id is not None and header.team_id != ctx.team_id:
        return None
    return header
