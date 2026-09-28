"""Singleton periodic work that isn't ingestion: the pre-standup brief and retention purge."""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update

from recall.db.models import Brief, Chunk, Meeting, Segment, Team
from recall.db.session import session_scope
from recall.jobs import periodic
from recall.query import dates

log = logging.getLogger(__name__)

BRIEF_LEAD = dt.timedelta(minutes=10)


def brief_due(team: Team, now: dt.datetime) -> dt.date | None:
    """The local date whose brief should be posted now, if we're inside the 10-minute pre-standup window."""
    if team.standup_time is None or not team.channel_id:
        return None
    local = now.astimezone(ZoneInfo(team.timezone))
    if not dates.is_working_day(local.date(), team.holidays):
        return None
    standup = dt.datetime.combine(local.date(), team.standup_time, ZoneInfo(team.timezone))
    return local.date() if standup - BRIEF_LEAD <= local < standup else None


async def post_due_briefs(now: dt.datetime | None = None) -> list[str]:
    from recall.pipeline.briefs import upsert_brief
    from recall.teams.bot import post_to_team

    now = now or dt.datetime.now(dt.UTC)
    posted: list[str] = []
    async with session_scope() as session:
        teams = (await session.scalars(select(Team).where(Team.standup_time.is_not(None)))).all()
        for team in teams:
            for_date = brief_due(team, now)
            if for_date is None:
                continue
            existing = await session.scalar(
                select(Brief).where(Brief.team_id == team.id, Brief.for_date == for_date)
            )
            if existing is not None and existing.posted_at is not None:
                continue
            brief = await upsert_brief(session, team, for_date)  # refresh: late meetings or closed items
            has_news = bool(brief.content["people"] or brief.content["due_items"])
            if has_news and await post_to_team(team, brief.rendered_card):
                posted.append(team.name)
            await session.execute(update(Brief).where(Brief.id == brief.id).values(posted_at=now))
    return posted


@periodic("standup_briefs", dt.timedelta(seconds=55))
async def standup_briefs() -> None:
    await post_due_briefs()


async def purge(now: dt.datetime | None = None) -> dict[str, int]:
    """Enforce per-team retention: media, then transcripts and chunks, then whole meetings (recaps, items)."""
    from recall.ingestion.storage import get_store

    now = now or dt.datetime.now(dt.UTC)
    counts = {"media": 0, "transcripts": 0, "meetings": 0}
    async with session_scope() as session:
        for team in (await session.scalars(select(Team))).all():
            media_cutoff = now - dt.timedelta(days=team.retention_media_days)
            transcript_cutoff = now - dt.timedelta(days=team.retention_transcript_days)
            recap_cutoff = now - dt.timedelta(days=team.retention_recap_days)
            old_media = (
                await session.scalars(
                    select(Meeting).where(
                        Meeting.team_id == team.id,
                        Meeting.media_path.is_not(None),
                        Meeting.started_at < media_cutoff,
                    )
                )
            ).all()
            for meeting in old_media:
                assert meeting.media_path is not None
                try:
                    await get_store().delete(meeting.media_path)
                except Exception:
                    log.warning("could not delete media for meeting %s", meeting.id)
                    continue
                meeting.media_path = None
                counts["media"] += 1
            old = select(Meeting.id).where(Meeting.team_id == team.id, Meeting.started_at < transcript_cutoff)
            result = await session.execute(delete(Segment).where(Segment.meeting_id.in_(old)))
            await session.execute(delete(Chunk).where(Chunk.meeting_id.in_(old)))
            counts["transcripts"] += result.rowcount or 0  # type: ignore[attr-defined]
            result = await session.execute(
                delete(Meeting).where(Meeting.team_id == team.id, Meeting.started_at < recap_cutoff)
            )
            counts["meetings"] += result.rowcount or 0  # type: ignore[attr-defined]
    return counts


@periodic("retention_purge", dt.timedelta(days=1))
async def retention_purge() -> None:
    counts = await purge()
    log.info("retention purge: %s", counts)


@periodic("ops_metrics", dt.timedelta(minutes=5))
async def ops_metrics() -> None:
    """One structured log record that the Azure Monitor alerts (infra/modules/alerts.bicep) evaluate."""
    from sqlalchemy import func

    from recall.db.models import GraphSubscription, Job, LlmUsage

    now = dt.datetime.now(dt.UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as session:
        depth = await session.scalar(select(func.count()).select_from(Job).where(Job.status == "queued"))
        failed = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == "failed", Job.updated_at >= now - dt.timedelta(minutes=5))
        )
        expires = await session.scalar(select(func.min(GraphSubscription.expires_at)))
        today = await session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(LlmUsage.ts >= day_start)
        )
        week = await session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(
                LlmUsage.ts >= day_start - dt.timedelta(days=7), LlmUsage.ts < day_start
            )
        )
    log.info(
        "ops_metrics",
        extra={
            "queue_depth": depth or 0,
            "failed_jobs_new": failed or 0,
            "subscription_hours_left": round((expires - now).total_seconds() / 3600, 1) if expires else 9999,
            "spend_today_usd": float(today or 0),
            "spend_avg_7d_usd": float(week or 0) / 7,
        },
    )
