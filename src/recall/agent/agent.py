"""The Retrieval Agent: a streaming Claude tool-use loop over recall.agent.tools, capped at a few tool calls."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from recall import llm
from recall.agent.prompts import AGENT_NO_TOOLS_NOTE, AGENT_SYSTEM
from recall.agent.tools import REF, TOOLS, ToolContext, run_tool
from recall.config import get_settings
from recall.domain import Citation

log = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    type: str  # "delta" | "reset" | "done"
    text: str = ""
    citations: list[Citation] | None = None


def _user_message(question: str, ctx: ToolContext) -> str:
    now = (ctx.now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(ctx.timezone))
    return (
        f"Today is {now:%A %Y-%m-%d}, {now:%H:%M} in the team's timezone ({ctx.timezone}). "
        f"The asker is {ctx.principal.display_name}.\n\nQuestion: {question}"
    )


def finalize(text: str, ctx: ToolContext) -> tuple[str, list[Citation]]:
    """Replace [m2@252000] refs with [1], [2]... and return the matching citations."""
    order: dict[tuple[str, int], int] = {}
    citations: list[Citation] = []

    def sub(match: Any) -> str:
        key = (match[1], int(match[2]))
        if key not in order:
            cite = ctx.citation(*key)
            if cite is None:
                return ""
            citations.append(cite)
            order[key] = len(citations)
        return f"[{order[key]}]"

    return REF.sub(sub, text).replace(" .", ".").strip(), citations


async def run_agent(question: str, ctx: ToolContext, effort: str | None = None) -> AsyncIterator[AgentEvent]:
    s = get_settings()
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_message(question, ctx)}]
    tool_calls = 0
    client = llm.get_client()
    while True:
        params: dict[str, Any] = {
            "model": s.agent_model,
            "max_tokens": s.agent_max_tokens,
            "system": [{"type": "text", "text": AGENT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            "tools": TOOLS,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": effort or s.agent_effort},
        }
        if tool_calls >= s.agent_max_tool_calls:
            params["tool_choice"] = {"type": "none"}
        async with client.messages.stream(**params) as stream:
            async for event in stream:
                delta: Any = getattr(event, "delta", None)
                if event.type == "content_block_delta" and getattr(delta, "type", "") == "text_delta":
                    yield AgentEvent("delta", delta.text)
            message: Any = await stream.get_final_message()
        await llm.record_usage("agent", s.agent_model, message.usage, team_id=ctx.team_id)

        tool_uses = [b for b in message.content if getattr(b, "type", "") == "tool_use"]
        if message.stop_reason == "tool_use" and tool_uses:
            yield AgentEvent("reset")
            messages.append({"role": "assistant", "content": message.content})
            results: list[dict[str, Any]] = []
            for block in tool_uses:
                # Tools share one DB session, so they run one after another; each is a single indexed query.
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": await run_tool(ctx, block.name, block.input),
                    }
                )
            tool_calls += len(tool_uses)
            if tool_calls >= s.agent_max_tool_calls:
                results.append({"type": "text", "text": AGENT_NO_TOOLS_NOTE})
            messages.append({"role": "user", "content": results})
            continue

        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        answer, citations = finalize(text, ctx)
        yield AgentEvent("done", answer, citations)
        return
