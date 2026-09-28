from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from typing import Any

import pytest

from recall.agent.service import ask
from recall.db.models import AnswerCache, AuditLog, LlmUsage, Person, Team
from recall.db.session import session_scope
from recall.query.auth import Forbidden, Principal, load_principal
from tests.conftest import load_recap, recap_handler
from tests.fakes import FakeAnthropic, FakeMessage, text_message, tool_message
from tests.helpers import DEV, MONDAY, add_prior_open_item, api_client, get, ingest_fixture


async def principal_for(person_id: Any) -> Principal:
    async with session_scope() as session:
        person = await session.get(Person, person_id)
        assert person is not None
        return await load_principal(session, person)


@pytest.fixture
async def ingested(fake_claude: FakeAnthropic, team_and_people: dict[str, Any]) -> dict[str, Any]:
    prior = await add_prior_open_item(team_and_people["team_id"], team_and_people["priya"])
    fake_claude.handler = recap_handler(load_recap(str(prior)))
    meeting_id = await ingest_fixture(team_and_people["team_id"])
    fake_claude.calls.clear()
    return {**team_and_people, "meeting_id": meeting_id}


def agent_script(*turns: FakeMessage) -> Any:
    """Handler that plays agent turns in order and fails loudly if the agent calls more than expected."""
    queue = list(turns)

    def handle(params: dict[str, Any]) -> FakeMessage:
        assert queue, "unexpected extra model call"
        return queue.pop(0)

    return handle


async def test_fast_path_standup_brief_has_no_model_call(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    hannah = await principal_for(ingested["hannah"])
    answer = await ask("What did everyone do in yesterday's standup?", hannah, now=MONDAY)
    assert answer.intent == "standup_brief"
    assert fake_claude.calls == []
    assert "Standup, Fri 25 Sep" in answer.answer
    assert "Private endpoint DNS" in answer.answer
    assert len(answer.citations) == 5
    assert answer.citations[0].label.startswith("Platform Standup · Fri 25 Sep · ")
    assert answer.card and answer.card["type"] == "AdaptiveCard"


async def test_fast_path_person_actions_and_recap(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    hannah = await principal_for(ingested["hannah"])
    person = await ask("What is Diego working on?", hannah, now=MONDAY)
    assert person.intent == "person_recent" and "SSO" in person.answer

    priya = await principal_for(ingested["priya"])
    mine = await ask("/mine", priya, now=MONDAY)
    assert mine.intent == "my_actions"
    assert "rollback runbook" in mine.answer
    assert "Migrate the billing service" not in mine.answer  # closed by Friday's standup

    recap = await ask("/recap", hannah, now=MONDAY)
    assert recap.intent == "meeting_recap" and "Redis" in recap.answer
    assert fake_claude.calls == []


async def test_open_question_uses_agent_with_citations(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    captured: dict[str, str] = {}

    def second_turn(params: dict[str, Any]) -> FakeMessage:
        # the agent saw the search results; cite the first ref it was given
        result = json.loads(params["messages"][-1]["content"][0]["content"])
        ref = result["results"][0]["ref"]
        captured["ref"] = ref
        return text_message(f"- The team dropped the Redis cache because Postgres p99 was 4 ms [{ref}].")

    turns = iter([tool_message(("search", {"query": "Redis cache decision"}))])
    fake_claude.handler = lambda params: next(turns, None) or second_turn(params)

    hannah = await principal_for(ingested["hannah"])
    answer = await ask("Why did we drop the Redis cache?", hannah, ingested["team_id"], now=MONDAY)
    assert answer.intent == "open_question"
    assert answer.answer.endswith("[1].")
    assert "[m" not in answer.answer
    assert len(answer.citations) == 1 and answer.citations[0].meeting_id == ingested["meeting_id"]
    first, second = fake_claude.calls
    assert first["model"] == "claude-sonnet-5"
    assert first["output_config"]["effort"] == "low"
    assert first["max_tokens"] == 1024
    assert [t["name"] for t in first["tools"]] == [t["name"] for t in second["tools"]]
    assert "Why did we drop the Redis cache?" in first["messages"][0]["content"]
    assert await get(AuditLog, action="agent_fallthrough")


async def test_answer_cache_hit_and_invalidation(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    fake_claude.handler = agent_script(
        tool_message(("search", {"query": "analytics sign-off"})),
        text_message("- Hannah will find out whether analytics must sign off."),
    )
    hannah = await principal_for(ingested["hannah"])
    team_id = ingested["team_id"]
    first = await ask("Does analytics need to sign off on the rollout?", hannah, team_id, now=MONDAY)
    assert not first.cached and len(fake_claude.calls) == 2

    second = await ask("does analytics need to sign off on the rollout", hannah, team_id, now=MONDAY)
    assert second.cached and second.answer == first.answer
    assert len(fake_claude.calls) == 2  # no model call on a cache hit
    assert len(await get(AnswerCache, team_id=team_id)) == 1

    fake_claude.handler = recap_handler(load_recap())
    await ingest_fixture(
        team_id, "planning_meeting.vtt", "Platform Standup", dt.datetime(2026, 9, 28, 13, 30, tzinfo=dt.UTC)
    )
    assert await get(AnswerCache, team_id=team_id) == []  # a newly indexed meeting clears the team's cache


async def test_escalates_effort_once_when_nothing_found(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    fake_claude.handler = agent_script(
        tool_message(("search", {"query": "kubernetes upgrade"})),
        text_message("I couldn't find that."),
        tool_message(("search", {"query": "k8s cluster upgrade"})),
        text_message("No meeting discussed a Kubernetes upgrade."),
    )
    # empty search results: point the search at a date range with no meetings
    hannah = await principal_for(ingested["hannah"])

    from recall.agent import tools

    real = tools.hybrid_search

    async def empty_search(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    tools.hybrid_search = empty_search  # type: ignore[assignment]
    try:
        answer = await ask(
            "Did we talk about the kubernetes upgrade?", hannah, ingested["team_id"], now=MONDAY
        )
    finally:
        tools.hybrid_search = real
    efforts = [c["output_config"]["effort"] for c in fake_claude.calls]
    assert efforts == ["low", "low", "medium", "medium"]
    assert answer.answer == "No meeting discussed a Kubernetes upgrade."
    assert await get(AnswerCache) == []  # "nothing found" answers aren't cached


async def test_tool_budget_forces_an_answer(fake_claude: FakeAnthropic, ingested: dict[str, Any]) -> None:
    fake_claude.handler = agent_script(
        tool_message(("search", {"query": "a"}), ("search", {"query": "b"})),
        tool_message(("search", {"query": "c"}), ("list_meetings", {})),
        text_message("- done"),
    )
    hannah = await principal_for(ingested["hannah"])
    await ask("Tell me about deployment pipelines", hannah, ingested["team_id"], now=MONDAY)
    last = fake_claude.calls[-1]
    assert last["tool_choice"] == {"type": "none"}  # 4 tool calls used: answer now
    assert len(fake_claude.calls) == 3


async def test_prompt_cache_reads_on_repeated_agent_calls(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    fake_claude.handler = lambda params: text_message("- nothing relevant")
    hannah = await principal_for(ingested["hannah"])
    await ask("first open question about vendors", hannah, ingested["team_id"], now=MONDAY)
    await ask(
        "second open question about contracts",
        hannah,
        ingested["team_id"],
        now=MONDAY + dt.timedelta(minutes=3),
    )
    rows = [u for u in await get(LlmUsage) if u.route == "agent"]
    assert rows[0].cache_write_tokens > 0
    assert all(r.cache_read_tokens > 0 for r in rows[1:])  # system + tools prefix is byte-stable


async def test_over_budget_falls_back_to_search_only(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    async with session_scope() as session:
        team = await session.get(Team, ingested["team_id"])
        assert team is not None
        team.monthly_budget_usd = Decimal("0.01")
        session.add(
            LlmUsage(route="agent", model="claude-sonnet-5", team_id=team.id, cost_usd=Decimal("0.02"))
        )
    hannah = await principal_for(ingested["hannah"])
    answer = await ask("Why did we drop the Redis cache?", hannah, ingested["team_id"], now=MONDAY)
    assert answer.intent == "search_only"
    assert "budget" in answer.answer and answer.citations
    assert fake_claude.calls == []

    brief = await ask("what did everyone do yesterday", hannah, ingested["team_id"], now=MONDAY)
    assert brief.intent == "standup_brief"  # fast path still works

    # summarization continues while over budget
    fake_claude.handler = recap_handler(load_recap())
    meeting_id = await ingest_fixture(
        ingested["team_id"], started_at=dt.datetime(2026, 9, 28, 13, 30, tzinfo=dt.UTC)
    )
    assert fake_claude.calls and (await get(LlmUsage, meeting_id=meeting_id))


async def test_budget_alert_queued_once_at_80_percent(
    fake_claude: FakeAnthropic, ingested: dict[str, Any]
) -> None:
    from recall import llm
    from recall.db.models import Job
    from tests.fakes import FakeUsage

    async with session_scope() as session:
        team = await session.get(Team, ingested["team_id"])
        assert team is not None
        team.monthly_budget_usd = Decimal("0.05")
    usage = FakeUsage(input_tokens=20_000, output_tokens=0)  # $0.04 on Sonnet 5
    await llm.record_usage("agent", "claude-sonnet-5", usage, team_id=ingested["team_id"])
    await llm.record_usage("agent", "claude-sonnet-5", usage, team_id=ingested["team_id"])
    assert len(await get(Job, kind="budget_alert")) == 1


async def test_non_member_cannot_query_team(ingested: dict[str, Any]) -> None:
    async with session_scope() as session:
        outsider = Person(display_name="Out Sider", email="out@contoso.com")
        session.add(outsider)
        await session.flush()
        principal = await load_principal(session, outsider)
    with pytest.raises(Forbidden):
        await ask("what happened yesterday", principal, ingested["team_id"], now=MONDAY)
    answer = await ask("/recap", principal, now=MONDAY)
    assert "couldn't find" in answer.answer  # sees nothing it didn't attend


async def test_rest_ask_json_and_sse(fake_claude: FakeAnthropic, ingested: dict[str, Any]) -> None:
    fake_claude.handler = lambda params: text_message("- Streaming works, one chunk at a time.")
    headers = {**DEV, "X-Dev-User": "hannah.lee@contoso.com"}
    async with api_client() as client:
        resp = await client.post("/v1/ask", headers=headers, json={"question": "/standup"})
        assert resp.status_code == 200
        assert resp.json()["intent"] == "standup_brief" and resp.json()["citations"]

        resp = await client.post(
            "/v1/ask",
            headers={**headers, "Accept": "text/event-stream"},
            json={"question": "Tell me about streaming", "team_id": str(ingested["team_id"])},
        )
        assert resp.status_code == 200
        events = re.findall(r"event: (\w+)", resp.text)
        assert "delta" in events and events[-1] == "answer"

        resp = await client.post("/v1/ask", json={"question": "hi"})
        assert resp.status_code == 401
        # the deploy workflow's smoke test: service token + a fast-path question, no model call
        calls = len(fake_claude.calls)
        resp = await client.post(
            "/v1/ask", headers={"Authorization": "Bearer service-secret"}, json={"question": "/mine"}
        )
        assert resp.status_code == 200 and '"intent":"my_actions"' in resp.text
        assert len(fake_claude.calls) == calls
        resp = await client.get("/v1/admin/usage", headers={"Authorization": "Bearer service-secret"})
        assert resp.status_code == 200 and resp.json()["breakdown"]
        resp = await client.get("/v1/admin/usage", headers={**DEV, "X-Dev-User": "priya.shah@contoso.com"})
        assert resp.status_code == 403
