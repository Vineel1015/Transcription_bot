from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from recall.agent.router import route_by_rules
from recall.agent.tools import TOOLS
from recall.domain import RecapOut
from recall.llm import compute_cost
from recall.pipeline.speakers import PersonRef, SpeakerMatcher
from recall.query.search import rrf_merge
from tests.conftest import load_recap
from tests.fakes import FakeUsage

PRIYA = PersonRef(uuid.uuid4(), "Priya Shah", "priya.shah@contoso.com", "e-priya", ("P. Shah",))
PRIYA_K = PersonRef(uuid.uuid4(), "Priya Kumar", "priya.kumar@contoso.com", "e-pk")
MARCUS = PersonRef(uuid.uuid4(), "Marcus Chen", "marcus.chen@contoso.com", "e-marcus")
DIEGO = PersonRef(uuid.uuid4(), "Diego Ramirez", "diego@contoso.com", None)
PEOPLE = [PRIYA, PRIYA_K, MARCUS, DIEGO]


def test_speaker_matching_order() -> None:
    matcher = SpeakerMatcher(
        PEOPLE,
        participants=[{"entra_id": "e-marcus", "name": "M. Chen (Guest)"}],
        local_ids={PRIYA.id, MARCUS.id, DIEGO.id},
        overrides={"Speaker 2": str(DIEGO.id)},
    )
    assert matcher.match("Speaker 2") == DIEGO.id  # override
    assert matcher.match("M. Chen (Guest)") == MARCUS.id  # Entra id via Graph participants
    assert matcher.match("priya.shah@contoso.com") == PRIYA.id  # email
    assert matcher.match("Shah, Priya") == PRIYA.id  # display name, "Last, First"
    assert matcher.match("P. Shah") == PRIYA.id  # alias
    assert matcher.match("Diego Ramírez") == DIEGO.id  # fuzzy (accent)
    assert matcher.match("Speaker 1") is None
    assert matcher.match("Someone Else") is None


def test_first_name_only_when_unique_locally() -> None:
    matcher = SpeakerMatcher(PEOPLE, local_ids={PRIYA.id, MARCUS.id})
    assert matcher.match("Marcus") == MARCUS.id
    assert matcher.match("Priya") == PRIYA.id  # Priya Kumar isn't on this team
    assert SpeakerMatcher(PEOPLE, local_ids={PRIYA.id, PRIYA_K.id}).match("Priya") is None


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("/standup", "standup_brief"),
        ("/mine", "my_actions"),
        ("/recap", "meeting_recap"),
        ("What did we cover in yesterday's standup?", "standup_brief"),
        ("what did everyone work on yesterday", "standup_brief"),
        ("What are my open action items?", "my_actions"),
        ("What is Marcus working on?", "person_recent"),
        ("What did Diego say he was blocked on yesterday?", "person_recent"),
        ("Recap the last meeting", "meeting_recap"),
        ("Why did we drop the Redis cache?", "open_question"),
        ("When did we decide to move nightly jobs?", "open_question"),
        ("What's the status of the SSO rollout?", "open_question"),
    ],
)
def test_router_rules(question: str, intent: str) -> None:
    assert route_by_rules(question, PEOPLE).intent == intent


def test_router_is_ambiguous_when_signals_conflict() -> None:
    routed = route_by_rules("What did Marcus say in the standup and what are my action items", PEOPLE)
    assert routed.intent == ""  # caller asks Haiku
    assert set(routed.candidates) == {"my_actions", "person_recent"}


def test_router_person_and_date() -> None:
    routed = route_by_rules("What did Marcus do yesterday?", PEOPLE)
    assert routed.intent == "person_recent" and routed.person == MARCUS
    assert routed.date_expression == "yesterday"


def test_rrf_merge() -> None:
    merged = rrf_merge([1, 2, 3], [3, 4, 1])
    ids = [i for i, _ in merged]
    assert ids[:2] == [1, 3]  # present in both lists
    assert set(ids) == {1, 2, 3, 4}


def test_recap_fixture_validates() -> None:
    recap = RecapOut.model_validate(load_recap())
    assert recap.kind == "standup"
    assert recap.action_items[0].due is not None


def test_tool_schemas_are_stable_and_strict() -> None:
    names = [t["name"] for t in TOOLS]
    assert names == [
        "resolve_dates",
        "list_meetings",
        "get_recap",
        "get_person_updates",
        "get_action_items",
        "search",
        "get_excerpt",
    ]
    assert all(t["input_schema"]["additionalProperties"] is False for t in TOOLS)


def test_cost_computation() -> None:
    usage = FakeUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=1_000_000)
    # Sonnet 5: $2 in, $10 out, cache reads at 10% of input
    assert compute_cost("claude-sonnet-5", usage) == Decimal("3.200000")
    assert compute_cost("claude-sonnet-5", usage, batch=True) == Decimal("1.600000")
    assert compute_cost("unknown-model", usage) == Decimal(0)
