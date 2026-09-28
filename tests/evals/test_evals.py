"""Real-API evaluations (`make eval`). Skipped without ANTHROPIC_API_KEY. They cost a few cents per run.

Summarizer: on the standup fixture, every expected action item is found with the right owner, no owner is invented,
blockers are attributed to the right people, and the prior open item is closed.
Agent: six scripted questions whose answers must contain expected facts and citations."""

from __future__ import annotations

import os
import re
from typing import Any

import pytest

from recall import llm
from recall.agent.service import ask
from recall.db.models import ActionItem, Person, PersonUpdate
from recall.db.session import session_scope
from recall.query.auth import load_principal
from tests.helpers import MONDAY, add_prior_open_item, get, ingest_fixture

API_KEY = os.environ.get("RECALL_EVAL_API_KEY") or os.environ.get("ANTHROPIC_API_KEY_REAL", "")
pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not API_KEY, reason="set ANTHROPIC_API_KEY_REAL (or RECALL_EVAL_API_KEY) to run evals"
    ),
]

EXPECTED_ITEMS = [  # (pattern in description, acceptable owners)
    (r"rollback runbook", {"priya"}),
    (r"flaky|checkout.*test", {"marcus", "diego"}),
    (r"azure support|ticket", {"aisha"}),
]
PARTICIPANTS = {"hannah", "priya", "marcus", "aisha", "diego"}


@pytest.fixture
async def real_claude() -> Any:
    import anthropic

    llm.set_client(anthropic.AsyncAnthropic(api_key=API_KEY))
    yield
    llm.set_client(None)


@pytest.fixture
async def summarized(real_claude: Any, team_and_people: dict[str, Any]) -> dict[str, Any]:
    prior = await add_prior_open_item(team_and_people["team_id"], team_and_people["priya"])
    meeting_id = await ingest_fixture(team_and_people["team_id"])
    return {**team_and_people, "prior": prior, "meeting_id": meeting_id}


def _who(people: dict[str, Any], person_id: Any) -> str | None:
    return next((k for k, v in people.items() if v == person_id), None)


async def test_summarizer_standup(summarized: dict[str, Any]) -> None:
    items = await get(ActionItem, meeting_id=summarized["meeting_id"])
    for pattern, owners in EXPECTED_ITEMS:
        matches = [i for i in items if re.search(pattern, i.description, re.I)]
        assert matches, f"missing action item matching {pattern!r}: {[i.description for i in items]}"
        assert any(_who(summarized, i.owner_id) in owners for i in matches), (
            pattern,
            [i.owner_text for i in matches],
        )
    runbook = next(i for i in items if re.search("rollback runbook", i.description, re.I))
    assert runbook.due_date is not None and runbook.due_date.isoformat() == "2026-09-30"  # "by Wednesday"
    for item in items:  # no invented owners
        assert item.owner_id is None or _who(summarized, item.owner_id) in PARTICIPANTS

    updates = {
        _who(summarized, u.person_id): u for u in await get(PersonUpdate, meeting_id=summarized["meeting_id"])
    }
    assert any("dns" in b.lower() for b in updates["aisha"].blockers)
    assert any("sso" in b.lower() or "account" in b.lower() for b in updates["diego"].blockers)
    assert not updates["priya"].blockers and not updates["marcus"].blockers

    [prior] = await get(ActionItem, id=summarized["prior"])
    assert prior.status == "done"


QUESTIONS = [
    ("What did everyone do in yesterday's standup?", [r"Priya", r"Aisha", r"DNS"]),
    ("Why did we decide to drop the Redis cache?", [r"p99|4 ?ms|load test", r"Postgres"]),
    ("What is Diego blocked on?", [r"SSO|test accounts"]),
    ("Who is fixing the flaky checkout tests and by when?", [r"Marcus", r"Diego", r"Monday|28"]),
    ("When does the old billing cluster go read-only?", [r"Monday"]),
    ("Is there an open question about the address form rollout?", [r"analytics"]),
]


@pytest.mark.parametrize(("question", "facts"), QUESTIONS)
async def test_agent_questions(summarized: dict[str, Any], question: str, facts: list[str]) -> None:
    async with session_scope() as session:
        hannah = await session.get(Person, summarized["hannah"])
        assert hannah is not None
        principal = await load_principal(session, hannah)
    answer = await ask(question, principal, summarized["team_id"], now=MONDAY)
    for fact in facts:
        assert re.search(fact, answer.answer, re.I), f"{question!r}: expected /{fact}/ in {answer.answer!r}"
    assert answer.citations, f"{question!r}: no citations in {answer.answer!r}"
    assert all(c.meeting_id == summarized["meeting_id"] for c in answer.citations)
