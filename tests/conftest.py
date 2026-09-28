"""Test setup: a real Postgres (TEST_DATABASE_URL, or an embedded pgserver instance), migrated once per session;
every test starts from empty tables. Claude, Graph and the Bot connector are faked."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
_pg_dir: str | None = None


def _database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    global _pg_dir
    import pgserver

    _pg_dir = tempfile.mkdtemp(prefix="recall-pg-")
    server = pgserver.get_server(_pg_dir, cleanup_mode="stop")
    server.psql("CREATE DATABASE recall_test;")
    uri = server.get_uri("recall_test")
    return uri.replace("postgresql://", "postgresql+asyncpg://", 1)


DATABASE_URL = _database_url()
# evals (tests/evals) use the real key; everything else runs against a fake client
os.environ.setdefault("ANTHROPIC_API_KEY_REAL", os.environ.get("ANTHROPIC_API_KEY", ""))
os.environ.update(
    {
        "DATABASE_URL": DATABASE_URL,
        "EMBEDDINGS_PROVIDER": "hash",
        "DEV_AUTH": "true",
        "DEV_TOKEN": "dev",
        "SERVICE_TOKEN": "service-secret",
        "RUN_WORKERS": "false",
        "RUN_MIGRATIONS_ON_STARTUP": "false",
        "ANTHROPIC_API_KEY": "test-key",
        "GRAPH_CLIENT_STATE": "test-client-state",
        "MEDIA_LOCAL_DIR": tempfile.mkdtemp(prefix="recall-media-"),
    }
)

from recall.config import get_settings  # noqa: E402

get_settings.cache_clear()

# Register job handlers and periodic tasks.
import recall.ingestion.graph  # noqa: E402
import recall.pipeline.run  # noqa: E402
import recall.scheduler  # noqa: E402
import recall.teams.bot  # noqa: E402, F401
from recall import llm  # noqa: E402
from recall.db.migrate import upgrade_head  # noqa: E402
from recall.db.models import Base  # noqa: E402
from recall.db.session import dispose_engine, session_scope  # noqa: E402
from recall.pipeline import embed  # noqa: E402
from tests.fakes import FakeAnthropic, FakeMessage, json_message  # noqa: E402

upgrade_head(DATABASE_URL)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if _pg_dir:
        shutil.rmtree(_pg_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
async def clean_db() -> AsyncIterator[None]:
    from sqlalchemy import text

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with session_scope() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    embed.set_embedder(None)
    yield
    llm.set_client(None)
    await dispose_engine()


@pytest.fixture
def fake_claude() -> Iterator[FakeAnthropic]:
    fake = FakeAnthropic()
    llm.set_client(fake)
    yield fake
    llm.set_client(None)


def load_recap(prior_id: str | None = None) -> dict[str, Any]:
    raw = (FIXTURES / "standup_recap.json").read_text()
    data: dict[str, Any] = json.loads(raw)
    data["completed_prior_action_item_ids"] = [prior_id] if prior_id else []
    return data


def recap_handler(recap: dict[str, Any]) -> Any:
    """Handler that answers summarizer calls (structured output) with `recap`."""

    def handle(params: dict[str, Any]) -> FakeMessage:
        return json_message(recap)

    return handle


@pytest.fixture
async def team_and_people() -> dict[str, Any]:
    """The Platform team and the five people in the standup fixture."""
    from recall.db.models import Person, Team, TeamMember

    async with session_scope() as session:
        team = Team(
            name="Platform",
            channel_id="19:platform@thread.tacv2",
            timezone="America/New_York",
            standup_time=dt.time(9, 30),
            title_patterns=["platform standup", "platform planning"],
        )
        session.add(team)
        await session.flush()
        people = {}
        for name, email, entra in [
            ("Hannah Lee", "hannah.lee@contoso.com", "976f4b31-fd01-4e0b-9178-29cc40c14438"),
            ("Priya Shah", "priya.shah@contoso.com", "11111111-1111-4111-8111-111111111111"),
            ("Marcus Chen", "marcus.chen@contoso.com", "22222222-2222-4222-8222-222222222222"),
            ("Aisha Okafor", "aisha.okafor@contoso.com", "33333333-3333-4333-8333-333333333333"),
            ("Diego Ramirez", "diego.ramirez@contoso.com", "44444444-4444-4444-8444-444444444444"),
        ]:
            person = Person(display_name=name, email=email, entra_id=entra, is_admin=name == "Hannah Lee")
            session.add(person)
            await session.flush()
            session.add(TeamMember(team_id=team.id, person_id=person.id))
            people[name.split()[0].lower()] = person.id
        return {"team_id": team.id, **people}


def new_id() -> uuid.UUID:
    return uuid.uuid4()
