"""`make demo`: seed a team and five people, ingest the standup and planning fixtures (dated the previous working
day), print the standup recap, then answer one fast-path question and one open question with citations.

Needs ANTHROPIC_API_KEY. Safe to re-run: seeding and ingestion are idempotent."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TZ = "America/New_York"
PEOPLE = [
    ("Hannah Lee", "hannah.lee@contoso.com", True),
    ("Priya Shah", "priya.shah@contoso.com", False),
    ("Marcus Chen", "marcus.chen@contoso.com", False),
    ("Aisha Okafor", "aisha.okafor@contoso.com", False),
    ("Diego Ramirez", "diego.ramirez@contoso.com", False),
]


def hr(title: str) -> None:
    print(f"\n{'─' * 8} {title} {'─' * max(4, 70 - len(title))}")


async def seed() -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    from recall.db.models import Person, Team, TeamMember
    from recall.db.session import session_scope

    async with session_scope() as session:
        team = await session.scalar(select(Team).where(Team.name == "Platform"))
        if team is None:
            team = Team(
                name="Platform",
                timezone=TZ,
                standup_time=dt.time(9, 30),
                title_patterns=["platform standup", "platform planning"],
            )
            session.add(team)
            await session.flush()
        people = {}
        for name, email, admin in PEOPLE:
            person = await session.scalar(select(Person).where(Person.email == email))
            if person is None:
                person = Person(display_name=name, email=email, is_admin=admin)
                session.add(person)
                await session.flush()
                session.add(TeamMember(team_id=team.id, person_id=person.id))
            people[name.split()[0].lower()] = person.id
        return team.id, people


async def ingest(team_id: uuid.UUID, people: dict[str, uuid.UUID], day: dt.date) -> uuid.UUID:
    from recall.config import get_settings
    from recall.db.models import ActionItem, Meeting
    from recall.db.session import session_scope
    from recall.ingestion.vtt import parse_vtt
    from recall.jobs import drain
    from recall.pipeline.run import create_meeting

    get_settings().use_batch_api = False  # the demo summarizes everything in real time
    zone = ZoneInfo(TZ)
    standup_at = dt.datetime.combine(day, dt.time(9, 30), zone)
    async with session_scope() as session:
        prior = await session.scalar(select(Meeting).where(Meeting.external_id == f"demo:prior:{day}"))
        if prior is None:
            prior = Meeting(
                team_id=team_id,
                title="Platform Standup",
                kind="standup",
                source="upload",
                external_id=f"demo:prior:{day}",
                started_at=standup_at - dt.timedelta(days=1),
                status="indexed",
            )
            session.add(prior)
            await session.flush()
            session.add(
                ActionItem(
                    meeting_id=prior.id,
                    description="Migrate the billing service to Postgres 16",
                    owner_id=people["priya"],
                    owner_text="Priya Shah",
                )
            )
        for name, title, at in [
            ("standup_2026-09-25.vtt", "Platform Standup", standup_at),
            ("planning_meeting.vtt", "Platform Planning", standup_at + dt.timedelta(hours=2)),
        ]:
            await create_meeting(
                session,
                title=title,
                source="upload",
                external_id=f"demo:{name}:{day}",
                team_id=team_id,
                started_at=at,
                cues=parse_vtt((FIXTURES / name).read_text()),
            )
    start = time.perf_counter()
    count = await drain()
    if count:
        print(f"processed {count} job(s) in {time.perf_counter() - start:.1f}s")
    async with session_scope() as session:
        meeting = await session.scalar(
            select(Meeting).where(Meeting.external_id == f"demo:standup_2026-09-25.vtt:{day}")
        )
        assert meeting is not None
        if meeting.status != "indexed":
            sys.exit(f"standup processing failed: {meeting.status} {meeting.error}")
        return meeting.id


async def main() -> None:
    import recall.pipeline.run  # noqa: F401  (registers job handlers)
    from recall.agent.service import ask
    from recall.config import get_settings
    from recall.db.migrate import upgrade_head_async
    from recall.db.models import LlmUsage, Person
    from recall.db.session import session_scope
    from recall.query import dates
    from recall.query.auth import Principal, load_principal
    from recall.query.meetings import get_recap

    if not get_settings().anthropic_api_key:
        sys.exit("Set ANTHROPIC_API_KEY first (in .env or the environment).")
    await upgrade_head_async()
    team_id, people = await seed()
    day = dates.previous_working_day(dates.local_today(TZ))
    hr(f"Ingesting fixtures as meetings on {day:%a %d %b}")
    meeting_id = await ingest(team_id, people, day)

    async with session_scope() as session:
        recap = await get_recap(session, Principal.service(), meeting_id)
        hannah = await session.get(Person, people["hannah"])
        assert recap is not None and hannah is not None
        principal = await load_principal(session, hannah)
    hr("Standup recap")
    print(recap["overview"])
    for d in recap["decisions"]:
        print(f"  decision: {d['decision']}")
    for a in recap["action_items"]:
        print(
            f"  action:   {a['description']} — {a['owner'] or 'unassigned'}{' (due ' + a['due'] + ')' if a['due'] else ''}"
        )
    for u in recap["person_updates"]:
        blockers = f" | BLOCKED: {'; '.join(u['blockers'])}" if u["blockers"] else ""
        print(f"  {u['name']}: done {len(u['done'])}, next {len(u['next'])}{blockers}")

    for question in [
        "What did everyone do in yesterday's standup?",
        "Why did we decide to drop the Redis cache?",
    ]:
        hr(f"Q: {question}")
        start = time.perf_counter()
        answer = await ask(question, principal, team_id)
        elapsed = time.perf_counter() - start
        print(answer.answer)
        for i, c in enumerate(answer.citations, 1):
            print(f"  [{i}] {c.label}")
        print(f"  ({answer.intent}{', cached' if answer.cached else ''}, {elapsed * 1000:.0f} ms)")

    async with session_scope() as session:
        total = await session.scalar(select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)))
        calls = await session.scalar(select(func.count()).select_from(LlmUsage))
    hr("Cost")
    print(f"{calls} Claude call(s) recorded so far, ${float(total or 0):.4f} total")


if __name__ == "__main__":
    asyncio.run(main())
