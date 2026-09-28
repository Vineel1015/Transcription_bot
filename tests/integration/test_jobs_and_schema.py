from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection

from recall import jobs
from recall.db.models import Base, Job, Meeting, PeriodicRun
from recall.db.session import get_engine, session_scope
from recall.jobs import Periodic, Worker, drain, enqueue, run_periodic
from recall.scheduler import purge
from tests.helpers import get, ingest_fixture


async def test_models_match_migrations() -> None:
    def diff(conn: Connection) -> list[Any]:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return [d for d in compare_metadata(ctx, Base.metadata) if not _ignorable(d)]

    async with get_engine().connect() as conn:
        assert await conn.run_sync(diff) == []


def _ignorable(d: Any) -> bool:
    # alembic_version isn't a model; the generated tsvector column can't round-trip through reflection
    text = str(d)
    return "alembic_version" in text or "tsv" in text


async def test_singleton_runs_exactly_once_across_replicas() -> None:
    ran: list[str] = []

    async def slow() -> None:
        ran.append("x")
        await asyncio.sleep(0.2)

    task = Periodic("test_singleton", dt.timedelta(minutes=5), slow)
    now = dt.datetime.now(dt.UTC)
    # two "replicas" tick at the same moment, then again a few seconds later
    results = await asyncio.gather(run_periodic(task, now), run_periodic(task, now))
    later = await asyncio.gather(
        run_periodic(task, now + dt.timedelta(seconds=30)), run_periodic(task, now + dt.timedelta(seconds=31))
    )
    assert sorted(results) == [False, True] and later == [False, False]
    assert ran == ["x"]
    [row] = await get(PeriodicRun, name="test_singleton")
    assert row.run_count == 1
    assert await run_periodic(task, now + dt.timedelta(minutes=6))  # next interval runs again


async def test_every_singleton_job_once_per_tick() -> None:
    counts: dict[str, int] = {}
    originals = dict(jobs.PERIODICS)
    try:
        for name in originals:

            async def count(name: str = name) -> None:
                counts[name] = counts.get(name, 0) + 1
                await asyncio.sleep(0.05)

            jobs.PERIODICS[name] = Periodic(name, originals[name].interval, count)
        now = dt.datetime.now(dt.UTC)
        await asyncio.gather(jobs.tick(now), jobs.tick(now))
    finally:
        jobs.PERIODICS.clear()
        jobs.PERIODICS.update(originals)
    assert set(counts) >= {
        "standup_briefs",
        "graph_renew",
        "graph_poll",
        "batch_submit",
        "batch_collect",
        "retention_purge",
        "reap_stale_jobs",
    }
    assert all(v == 1 for v in counts.values()), counts


async def test_shutdown_returns_unfinished_job_to_queue() -> None:
    started = asyncio.Event()

    @jobs.handler("test_slow")
    async def slow(payload: dict[str, Any]) -> None:
        started.set()
        await asyncio.sleep(30)

    async with session_scope() as session:
        await enqueue(session, "test_slow", {})
    worker = Worker(concurrency=1, poll_seconds=0.05, grace_seconds=0.2)
    worker.start()
    await asyncio.wait_for(started.wait(), 5)
    await worker.stop()  # what SIGTERM triggers via the app lifespan
    [job] = await get(Job, kind="test_slow")
    assert job.status == "queued" and job.attempts == 0 and job.locked_at is None


async def test_retries_then_fails_and_marks_meeting(
    team_and_people: dict[str, Any], fake_claude: Any
) -> None:
    def boom(params: dict[str, Any]) -> Any:
        raise RuntimeError("model unavailable")

    fake_claude.handler = boom
    meeting_id = await ingest_fixture(team_and_people["team_id"], run=False)
    for _ in range(3):
        await drain()
        async with session_scope() as session:  # skip the backoff wait
            from sqlalchemy import update

            await session.execute(
                update(Job).values(run_after=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
            )
    [job] = await get(Job, kind="process_meeting")
    assert job.status == "failed" and job.attempts == 3 and "model unavailable" in (job.last_error or "")
    [meeting] = await get(Meeting, id=meeting_id)
    assert meeting.status == "failed"


async def test_retention_purge(team_and_people: dict[str, Any], fake_claude: Any) -> None:
    from recall.db.models import Chunk, Segment
    from tests.conftest import load_recap, recap_handler

    fake_claude.handler = recap_handler(load_recap())
    meeting_id = await ingest_fixture(team_and_people["team_id"])
    counts = await purge(dt.datetime(2026, 9, 25, tzinfo=dt.UTC) + dt.timedelta(days=200))
    assert counts["transcripts"] > 0 and counts["meetings"] == 0
    assert await get(Segment, meeting_id=meeting_id) == [] and await get(Chunk, meeting_id=meeting_id) == []
    counts = await purge(dt.datetime(2026, 9, 25, tzinfo=dt.UTC) + dt.timedelta(days=800))
    assert counts["meetings"] == 1
