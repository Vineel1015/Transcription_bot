"""Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED) and singleton periodic tasks
(pg_try_advisory_lock + a periodic_runs row), so any number of replicas can run workers safely."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import Job, PeriodicRun
from recall.db.session import get_engine, session_scope

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
FailureHook = Callable[[dict[str, Any], str], Awaitable[None]]

HANDLERS: dict[str, Handler] = {}
FAILURE_HOOKS: dict[str, FailureHook] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


def on_final_failure(kind: str) -> Callable[[FailureHook], FailureHook]:
    def register(fn: FailureHook) -> FailureHook:
        FAILURE_HOOKS[kind] = fn
        return fn

    return register


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    run_after: dt.datetime | None = None,
    max_attempts: int = 3,
) -> int | None:
    """Queue a job. Returns None when a job with the same dedupe_key already exists."""
    values: dict[str, Any] = {"kind": kind, "payload": payload, "max_attempts": max_attempts}
    if dedupe_key:
        values["dedupe_key"] = dedupe_key
    if run_after:
        values["run_after"] = run_after
    stmt = (
        insert(Job).values(**values).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(Job.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


@dataclass
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


_CLAIM = text(
    """
    UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_at = now(), updated_at = now()
    WHERE id = (
        SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()
        ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id, kind, payload, attempts, max_attempts
    """
)


async def claim_job() -> ClaimedJob | None:
    async with session_scope() as session:
        row = (await session.execute(_CLAIM)).first()
        return ClaimedJob(*row) if row else None


def backoff(attempts: int) -> dt.timedelta:
    return dt.timedelta(seconds=min(5 * 2**attempts, 600))


async def execute(job: ClaimedJob) -> None:
    """Run one claimed job and record the outcome. Cancellation returns the job to the queue."""
    fn = HANDLERS.get(job.kind)
    try:
        if fn is None:
            raise RuntimeError(f"no handler for job kind {job.kind!r}")
        await fn(job.payload)
    except asyncio.CancelledError:
        await requeue(job.id)
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        final = job.attempts >= job.max_attempts
        log.warning("job %s (%s) failed attempt %s: %s", job.id, job.kind, job.attempts, error)
        async with session_scope() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    status="failed" if final else "queued",
                    last_error=error,
                    locked_at=None,
                    run_after=func.now() + backoff(job.attempts),
                    updated_at=func.now(),
                )
            )
        hook = FAILURE_HOOKS.get(job.kind)
        if final and hook is not None:
            await hook(job.payload, error)
        return
    async with session_scope() as session:
        await session.execute(
            update(Job).where(Job.id == job.id).values(status="done", locked_at=None, updated_at=func.now())
        )


async def requeue(job_id: int) -> None:
    """Return an interrupted job to the queue without consuming an attempt."""
    async with session_scope() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(status="queued", attempts=Job.attempts - 1, locked_at=None, updated_at=func.now())
        )


async def drain(max_jobs: int = 1000) -> int:
    """Run queued jobs inline until the queue is empty (demo, tests, CLI)."""
    count = 0
    while count < max_jobs:
        job = await claim_job()
        if job is None:
            break
        await execute(job)
        count += 1
    return count


class Worker:
    """N concurrent job loops. stop() stops claiming, lets running jobs finish within the grace period,
    then cancels the rest, which returns them to the queue."""

    def __init__(self, concurrency: int = 4, poll_seconds: float = 1.0, grace_seconds: float = 30.0) -> None:
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self.grace_seconds = grace_seconds
        self._stopping = asyncio.Event()
        self._loops: list[asyncio.Task[None]] = []
        self._running: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        self._loops = [asyncio.create_task(self._loop(), name=f"worker-{i}") for i in range(self.concurrency)]

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                job = await claim_job()
            except Exception:
                log.exception("claiming a job failed")
                job = None
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), self.poll_seconds)
                continue
            task = asyncio.create_task(execute(job), name=f"job-{job.id}")
            self._running.add(task)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.wait({task})
            finally:
                self._running.discard(task)

    async def stop(self) -> None:
        self._stopping.set()
        running = set(self._running)
        if running:
            _, pending = await asyncio.wait(running, timeout=self.grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)
        for loop in self._loops:
            loop.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)


# -------------------------------------------------------------------------------------------------------------
# Singleton periodic tasks
# -------------------------------------------------------------------------------------------------------------


@dataclass
class Periodic:
    name: str
    interval: dt.timedelta
    fn: Callable[[], Awaitable[None]]


PERIODICS: dict[str, Periodic] = {}


def periodic(name: str, interval: dt.timedelta) -> Callable[[Callable[[], Awaitable[None]]], Any]:
    def register(fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        PERIODICS[name] = Periodic(name, interval, fn)
        return fn

    return register


def lock_key(name: str) -> int:
    return zlib.crc32(f"recall:{name}".encode())


async def run_periodic(task: Periodic, now: dt.datetime | None = None) -> bool:
    """Run `task` if its interval has elapsed and no other replica holds its lock. Returns True if it ran."""
    now = now or dt.datetime.now(dt.UTC)
    key = lock_key(task.name)
    async with get_engine().connect() as conn:
        got = await conn.scalar(select(func.pg_try_advisory_lock(key)))
        await conn.commit()
        if not got:
            return False
        try:
            last = await conn.scalar(select(PeriodicRun.last_run_at).where(PeriodicRun.name == task.name))
            await conn.commit()
            if last is not None and last + task.interval > now:
                return False
            try:
                await task.fn()
            except Exception:
                log.exception("periodic task %s failed", task.name)
            stmt = insert(PeriodicRun).values(name=task.name, last_run_at=now, run_count=1)
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={"last_run_at": now, "run_count": PeriodicRun.run_count + 1},
            )
            await conn.execute(stmt)
            await conn.commit()
            return True
        finally:
            await conn.execute(select(func.pg_advisory_unlock(key)))
            await conn.commit()


async def tick(now: dt.datetime | None = None) -> list[str]:
    ran = []
    for task in list(PERIODICS.values()):
        if await run_periodic(task, now):
            ran.append(task.name)
    return ran


class Scheduler:
    def __init__(self, tick_seconds: float) -> None:
        self.tick_seconds = tick_seconds
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await tick()
            except Exception:
                log.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self.tick_seconds)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task


@periodic("reap_stale_jobs", dt.timedelta(minutes=5))
async def reap_stale_jobs() -> None:
    """Jobs left 'running' by a crashed replica go back to the queue."""
    async with session_scope() as session:
        await session.execute(
            update(Job)
            .where(Job.status == "running", Job.locked_at < func.now() - dt.timedelta(minutes=15))
            .values(status="queued", locked_at=None, updated_at=func.now())
        )
