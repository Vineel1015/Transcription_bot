"""Thin layer over the Anthropic SDK: one client, per-call usage/cost logging to llm_usage, and team
budgets. Every Claude call in the app goes through here."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any, Protocol

import anthropic
from sqlalchemy import func, select

from recall.config import get_settings
from recall.db.models import LlmUsage, Team
from recall.db.session import session_scope

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


class UsageLike(Protocol):
    input_tokens: int
    output_tokens: int


def get_client() -> anthropic.AsyncAnthropic:
    """The shared async client. Tests replace it with set_client()."""
    global _client
    if _client is None:
        settings = get_settings()
        if settings.anthropic_provider == "foundry":
            _client = anthropic.AsyncAnthropicFoundry(
                api_key=settings.foundry_api_key, resource=settings.foundry_resource
            )
        else:
            _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None, max_retries=3)
    return _client


def set_client(client: Any) -> None:
    global _client
    _client = client


def compute_cost(model: str, usage: Any, *, batch: bool = False) -> Decimal:
    prices = get_settings().prices.get(model)
    if prices is None:
        log.warning("no price configured for model %s; recording cost as 0", model)
        return Decimal(0)
    per_in = Decimal(str(prices["input"])) / Decimal(1_000_000)
    per_out = Decimal(str(prices["output"])) / Decimal(1_000_000)
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = (
        usage.input_tokens * per_in
        + cache_write * per_in * Decimal("1.25")
        + cache_read * per_in * Decimal("0.1")
        + usage.output_tokens * per_out
    )
    total = Decimal(cost * Decimal("0.5") if batch else cost)
    return total.quantize(Decimal("0.000001"))


async def record_usage(
    route: str,
    model: str,
    usage: Any,
    *,
    team_id: uuid.UUID | None = None,
    meeting_id: uuid.UUID | None = None,
    batch: bool = False,
) -> Decimal:
    cost = compute_cost(model, usage, batch=batch)
    async with session_scope() as session:
        session.add(
            LlmUsage(
                route=route,
                model=model,
                team_id=team_id,
                meeting_id=meeting_id,
                input_tokens=usage.input_tokens,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                output_tokens=usage.output_tokens,
                batch=batch,
                cost_usd=cost,
            )
        )
    if team_id is not None:
        await _maybe_alert_budget(team_id)
    return cost


async def create(
    route: str,
    *,
    team_id: uuid.UUID | None = None,
    meeting_id: uuid.UUID | None = None,
    **params: Any,
) -> Any:
    """messages.create + usage logging."""
    response = await get_client().messages.create(**params)
    await record_usage(route, params["model"], response.usage, team_id=team_id, meeting_id=meeting_id)
    return response


def month_start(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_to_date_spend(team_id: uuid.UUID) -> Decimal:
    async with session_scope() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(
                LlmUsage.team_id == team_id, LlmUsage.ts >= month_start()
            )
        )
    return Decimal(total or 0)


async def budget_state(team_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """(month-to-date spend, monthly budget)."""
    async with session_scope() as session:
        budget = await session.scalar(select(Team.monthly_budget_usd).where(Team.id == team_id))
    return await month_to_date_spend(team_id), Decimal(budget if budget is not None else 0)


async def over_budget(team_id: uuid.UUID) -> bool:
    spend, budget = await budget_state(team_id)
    return budget > 0 and spend >= budget


async def _maybe_alert_budget(team_id: uuid.UUID) -> None:
    """At 80% of the monthly budget, queue one alert per team per month for the team's channel."""
    spend, budget = await budget_state(team_id)
    if budget <= 0 or spend < budget * Decimal("0.8"):
        return
    month = month_start().strftime("%Y-%m")
    from recall.jobs import enqueue

    async with session_scope() as session:
        team = await session.get(Team, team_id)
        if team is None or team.budget_alert_month == month:
            return
        team.budget_alert_month = month
        await enqueue(
            session,
            "budget_alert",
            {"team_id": str(team_id), "spend": str(spend), "budget": str(budget)},
            dedupe_key=f"budget_alert:{team_id}:{month}",
        )
