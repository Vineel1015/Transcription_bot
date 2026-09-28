"""`make bench`: check the performance targets against seeded data and report cost per meeting / question.

Seeds a separate "Bench" team with N meetings (default 10,000), each with a recap, five person updates, two action
items and five chunks. Chunk vectors are random unit vectors (this measures the database and indexes, not the
embedding model); queries use the configured embedder. Model-dependent rows need ANTHROPIC_API_KEY and are SKIP
otherwise. Use --cleanup to delete the bench team afterwards."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
import random
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select

ROOT = Path(__file__).resolve().parents[1]
BENCH_TEAM = "Bench"
NAMES = ["Ada Park", "Ben Ortiz", "Cleo Nakamura", "Dev Patel", "Eve Novak"]
TOPICS = [
    "billing migration",
    "checkout tests",
    "staging DNS",
    "SSO rollout",
    "session cache",
    "deploy pipeline",
    "mobile release",
    "alerting",
    "queue dead letters",
    "address form rollout",
]


def rand_unit(dim: int, rng: random.Random) -> list[float]:
    vec = [rng.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


async def seed(n: int) -> tuple[uuid.UUID, uuid.UUID]:
    from recall.config import get_settings
    from recall.db.models import ActionItem, Chunk, Meeting, Person, PersonUpdate, Recap, Team, TeamMember
    from recall.db.session import session_scope
    from recall.pipeline.briefs import upsert_brief

    dim = get_settings().embedding_dim
    rng = random.Random(7)
    async with session_scope() as session:
        team = await session.scalar(select(Team).where(Team.name == BENCH_TEAM))
        if team is None:
            team = Team(name=BENCH_TEAM, timezone="UTC", standup_time=dt.time(9, 30))
            session.add(team)
            await session.flush()
        people = []
        for name in NAMES:
            email = name.lower().replace(" ", ".") + "@bench.example"
            person = await session.scalar(select(Person).where(Person.email == email))
            if person is None:
                person = Person(display_name=name, email=email)
                session.add(person)
                await session.flush()
                session.add(TeamMember(team_id=team.id, person_id=person.id))
            people.append(person.id)
        existing = (
            await session.scalar(select(func.count()).select_from(Meeting).where(Meeting.team_id == team.id))
            or 0
        )
        team_id, asker = team.id, people[0]
    if existing >= n:
        print(f"bench team already has {existing} meetings")
        return team_id, asker

    today = dt.datetime.now(dt.UTC).replace(hour=9, minute=30, second=0, microsecond=0)
    print(f"seeding {n - existing} meetings…", flush=True)
    batch = 500
    for start in range(existing, n, batch):
        rows = []
        for i in range(start, min(n, start + batch)):
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "team_id": team_id,
                    "title": "Bench Standup",
                    "kind": "standup",
                    "source": "upload",
                    "external_id": f"bench:{i}",
                    "status": "indexed",
                    "started_at": today - dt.timedelta(days=1 + i // 5, hours=i % 5),
                }
            )
        async with session_scope() as session:
            await session.execute(insert(Meeting), rows)
            recaps, updates, items, chunks = [], [], [], []
            for m in rows:
                topic = rng.choice(TOPICS)
                recaps.append(
                    {
                        "meeting_id": m["id"],
                        "model": "bench",
                        "prompt_version": "bench",
                        "overview": f"The team discussed the {topic}.",
                        "topics": [],
                        "decisions": [],
                        "open_questions": [],
                    }
                )
                for j, pid in enumerate(people):
                    updates.append(
                        {
                            "meeting_id": m["id"],
                            "person_id": pid,
                            "speaker_label": NAMES[j],
                            "done": [f"worked on {rng.choice(TOPICS)}"],
                            "next": [f"continue {topic}"],
                            "blockers": [],
                            "cite_ms": j * 60_000,
                        }
                    )
                for j in range(2):
                    items.append(
                        {
                            "meeting_id": m["id"],
                            "description": f"Follow up on {rng.choice(TOPICS)}",
                            "owner_id": rng.choice(people),
                            "status": rng.choice(["open", "done", "done"]),
                            "cite_ms": j * 30_000,
                        }
                    )
                for j in range(5):
                    chunks.append(
                        {
                            "meeting_id": m["id"],
                            "kind": "segment_window",
                            "start_ms": j * 120_000,
                            "text": f"{NAMES[j]}: update on the {rng.choice(TOPICS)} and the {topic}.",
                            "embedding": rand_unit(dim, rng),
                        }
                    )
            await session.execute(insert(Recap), recaps)
            await session.execute(insert(PersonUpdate), updates)
            await session.execute(insert(ActionItem), items)
            await session.execute(insert(Chunk), chunks)
        print(f"  {min(n, start + batch)}/{n}", flush=True)
    async with session_scope() as session:
        team = await session.get(Team, team_id)
        assert team is not None
        await upsert_brief(session, team, dt.datetime.now(dt.UTC).date())
    return team_id, asker


async def timed(fn: Any, runs: int) -> list[float]:
    out = []
    for _ in range(runs):
        start = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


async def main() -> None:
    from recall.agent.service import ask_stream
    from recall.config import get_settings
    from recall.db.migrate import upgrade_head_async
    from recall.db.models import AnswerCache, AuditLog, LlmUsage, Meeting, Person, Team
    from recall.db.session import session_scope
    from recall.query.auth import load_principal
    from recall.query.search import hybrid_search

    parser = argparse.ArgumentParser()
    parser.add_argument("--meetings", type=int, default=10_000)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()

    await upgrade_head_async()
    team_id, asker = await seed(args.meetings)
    async with session_scope() as session:
        person = await session.get(Person, asker)
        assert person is not None
        principal = await load_principal(session, person)

    results: list[tuple[str, str, str, str]] = []

    async def fast() -> None:
        async for _ in ask_stream("What did everyone do in yesterday's standup?", principal, team_id):
            pass

    await fast()  # warm up (connection pool, embedder)
    fast_ms = await timed(fast, args.runs)
    results.append(
        (
            "Standup question (fast path) p50",
            "< 1500 ms",
            f"{statistics.median(fast_ms):.0f} ms",
            "PASS" if statistics.median(fast_ms) < 1500 else "FAIL",
        )
    )

    queries = [
        "billing migration",
        "why did staging DNS fail",
        "SSO rollout blockers",
        "checkout test flakiness",
        "deploy pipeline approval",
        "session cache decision",
    ]

    async def one_search(q: str) -> None:
        async with session_scope() as session:
            await hybrid_search(session, principal, q, team_id=team_id, k=5)

    await one_search("warm up")
    search_ms: list[float] = []
    for i in range(args.runs):
        search_ms += await timed(lambda i=i: one_search(queries[i % len(queries)]), 1)
    async with session_scope() as session:
        n = await session.scalar(select(func.count()).select_from(Meeting).where(Meeting.team_id == team_id))
    results.append(
        (
            f"Hybrid search over {n:,} meetings p50",
            "< 200 ms",
            f"{statistics.median(search_ms):.0f} ms",
            "PASS" if statistics.median(search_ms) < 200 else "FAIL",
        )
    )

    if get_settings().anthropic_api_key:
        firsts, totals = [], []
        for i in range(min(args.runs, 5)):
            async with session_scope() as session:
                await session.execute(delete(AnswerCache).where(AnswerCache.team_id == team_id))
            start = time.perf_counter()
            first = None
            async for event in ask_stream(
                f"What was said about the {queries[i % len(queries)]}?", principal, team_id
            ):
                if first is None and event.type in ("delta", "answer"):
                    first = time.perf_counter() - start
            totals.append(time.perf_counter() - start)
            firsts.append(first or totals[-1])
        results.append(
            (
                "Open question: first token p50",
                "< 2 s",
                f"{statistics.median(firsts):.2f} s",
                "PASS" if statistics.median(firsts) < 2 else "FAIL",
            )
        )
        results.append(
            (
                "Open question: complete p50",
                "< 6 s",
                f"{statistics.median(totals):.2f} s",
                "PASS" if statistics.median(totals) < 6 else "FAIL",
            )
        )
    else:
        results.append(("Open question latency", "< 2 s / < 6 s", "-", "SKIP (no ANTHROPIC_API_KEY)"))

    async with session_scope() as session:
        per_meeting_costs = (
            select(func.sum(LlmUsage.cost_usd).label("cost"))
            .where(LlmUsage.route.like("summarize%"), LlmUsage.meeting_id.is_not(None))
            .group_by(LlmUsage.meeting_id)
            .subquery()
        )
        per_meeting = await session.scalar(select(func.avg(per_meeting_costs.c.cost)))
        agent_cost = await session.scalar(
            select(func.sum(LlmUsage.cost_usd)).where(LlmUsage.route == "agent")
        )
        agent_questions = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "agent_fallthrough")
        )
    per_question = (agent_cost / agent_questions) if agent_cost is not None and agent_questions else None
    results.append(
        (
            "Avg cost per meeting summarized",
            "-",
            f"${float(per_meeting):.4f}" if per_meeting is not None else "no data",
            "INFO",
        )
    )
    results.append(
        (
            "Avg cost per agent question",
            "-",
            f"${float(per_question):.4f}" if per_question is not None else "no data",
            "INFO",
        )
    )
    results.append(("Standup recap time", "< 2 min", "see 'processed ... in' from make demo", "INFO"))

    width = max(len(r[0]) for r in results)
    print(f"\n{'Metric'.ljust(width)}  {'Target':<16} {'Result':<14} Status")
    for metric, target, value, status in results:
        print(f"{metric.ljust(width)}  {target:<16} {value:<14} {status}")

    if args.cleanup:
        async with session_scope() as session:
            await session.execute(delete(Team).where(Team.id == team_id))
        print("bench team deleted")
    if any(r[3] == "FAIL" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
