"""`recall-mcp`: read-only MCP server (stdio) exposing the Retrieval Agent's tools, so Claude Desktop and Claude
Code can query meeting memory. Runs as the person in MCP_USER_EMAIL (their normal permissions apply)."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from recall.agent.tools import ToolContext, run_tool
from recall.config import get_settings
from recall.db.models import Person
from recall.db.session import session_scope
from recall.query.auth import Principal, load_principal
from recall.query.meetings import team_timezone

mcp = MCPServer(
    "recall",
    instructions=(
        "Meeting memory for Microsoft Teams: recaps, decisions, action items and standup updates. Results carry refs "
        "like m2@252000 (meeting alias @ millisecond offset) that stay valid for the rest of the session."
    ),
)

_state: dict[str, Any] = {}


async def _principal() -> Principal:
    email = get_settings().mcp_user_email
    if not email:
        raise RuntimeError("set MCP_USER_EMAIL to the email of the Recall user this server acts as")
    async with session_scope() as session:
        person = await session.scalar(select(Person).where(Person.email == email.lower()))
        if person is None:
            raise RuntimeError(f"no Recall user with email {email}")
        return await load_principal(session, person)


async def _call(name: str, args: dict[str, Any], team_id: str | None = None) -> str:
    """Refs (m1, m2...) persist across calls within this MCP session."""
    principal = _state.get("principal") or await _principal()
    _state["principal"] = principal
    tid = uuid.UUID(team_id) if team_id else None
    async with session_scope() as session:
        tz, holidays = await team_timezone(session, tid) if tid else ("UTC", [])
        ctx: ToolContext = _state.get("ctx") or ToolContext(session, principal, tid, tz, holidays)
        ctx.session, ctx.team_id, ctx.timezone, ctx.holidays = session, tid, tz, holidays
        _state["ctx"] = ctx
        return await run_tool(ctx, name, {k: v for k, v in args.items() if v is not None})


@mcp.tool()
async def resolve_dates(expression: str, team_id: str | None = None) -> str:
    """Turn 'yesterday', 'last week', 'Tuesday' or a date into a start/end range (working days)."""
    return await _call("resolve_dates", {"expression": expression}, team_id)


@mcp.tool()
async def list_meetings(
    kind: str | None = None,
    start: str | None = None,
    end: str | None = None,
    participant: str | None = None,
    team_id: str | None = None,
) -> str:
    """List summarized meetings, newest first. Dates are YYYY-MM-DD."""
    return await _call(
        "list_meetings", {"kind": kind, "start": start, "end": end, "participant": participant}, team_id
    )


@mcp.tool()
async def get_recap(meeting: str, team_id: str | None = None) -> str:
    """Full recap of a meeting (ref like m2, or a meeting id)."""
    return await _call("get_recap", {"meeting": meeting}, team_id)


@mcp.tool()
async def get_person_updates(
    person: str, start: str | None = None, end: str | None = None, team_id: str | None = None
) -> str:
    """A person's done/next/blockers from recent meetings."""
    return await _call("get_person_updates", {"person": person, "start": start, "end": end}, team_id)


@mcp.tool()
async def get_action_items(
    status: str | None = None, owner: str | None = None, since: str | None = None, team_id: str | None = None
) -> str:
    """Action items by status (open, done, dropped, any) and owner."""
    return await _call("get_action_items", {"status": status, "owner": owner, "since": since}, team_id)


@mcp.tool()
async def search(
    query: str, start: str | None = None, end: str | None = None, k: int = 5, team_id: str | None = None
) -> str:
    """Hybrid keyword + semantic search over transcripts and recaps."""
    return await _call("search", {"query": query, "start": start, "end": end, "k": k}, team_id)


@mcp.tool()
async def get_excerpt(meeting: str, start_ms: int, end_ms: int, team_id: str | None = None) -> str:
    """Raw transcript lines (at most 3 minutes)."""
    return await _call("get_excerpt", {"meeting": meeting, "start_ms": start_ms, "end_ms": end_ms}, team_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
