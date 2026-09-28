"""Intent routing: regex/keyword rules first; one Haiku call only when the rules are ambiguous.

Intents: standup_brief, person_recent, my_actions, meeting_recap, open_question. The first four are answered
from precomputed data with no model call."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from recall import llm
from recall.config import get_settings
from recall.pipeline.speakers import PersonRef, normalize_name
from recall.query.dates import find_date_expression

INTENTS = ("standup_brief", "person_recent", "my_actions", "meeting_recap", "open_question")

_STANDUP = re.compile(r"\b(stand[\s-]?up|scrum|dsu)\b", re.I)
_EVERYONE = re.compile(
    r"\bwhat did (everyone|everybody|the team|we|people) (do|work on|get done|say)\b|\bteam('s)? updates?\b",
    re.I,
)
_MY_ACTIONS = re.compile(
    r"\b(my|mine)\b.*\b(action items?|tasks?|to-?dos?|follow[\s-]?ups?)\b|\bwhat (do|should) i (need|have) to do\b"
    r"|\bwhat('s| is) on my plate\b|\bmy open items\b",
    re.I,
)
_RECAP = re.compile(
    r"\b(recap|summary|summari[sz]e)\b.*\b(last|latest|previous|recent|today'?s?|yesterday'?s?) (meeting|call|sync|standup)\b"
    r"|\b(recap|summari[sz]e) (the|our) (last|latest|previous) (meeting|call)\b|^\s*recap\s*\??\s*$",
    re.I,
)
_PERSON_VERB = re.compile(
    r"\b(working on|work on|worked on|doing|did|done|up to|update|status|blocked|blockers?|said|say)\b", re.I
)
_WHY = re.compile(
    r"\b(why|how come|reason|decid|decision|when did we|who decided|what was the rationale)\b", re.I
)
_COMMAND = re.compile(r"^\s*/(standup|recap|mine)\b", re.I)


@dataclass
class Route:
    intent: str
    date_expression: str | None = None
    person: PersonRef | None = None
    source: str = "rules"
    candidates: list[str] = field(default_factory=list)


def _mentioned_people(question: str, people: list[PersonRef]) -> list[PersonRef]:
    """People named in the question by full name, alias, or a first name no one else shares."""
    q = f" {normalize_name(question)} "
    first_counts: dict[str, int] = {}
    for p in people:
        first = normalize_name(p.display_name).split(" ")[0]
        first_counts[first] = first_counts.get(first, 0) + 1

    def said(name: str) -> bool:
        return bool(name) and (f" {name} " in q or f" {name}s " in q)

    found = []
    for p in people:
        full = normalize_name(p.display_name)
        first = full.split(" ")[0]
        if (
            said(full)
            or any(said(normalize_name(a)) for a in p.aliases)
            or (len(first) > 2 and first_counts[first] == 1 and said(first))
        ):
            found.append(p)
    return found


def route_by_rules(question: str, people: list[PersonRef], asker_id: uuid.UUID | None = None) -> Route:
    """Route by rules. An empty intent means the rules were ambiguous and the caller may ask Haiku."""
    date_expr = find_date_expression(question)
    command = _COMMAND.match(question)
    if command:
        intent = {"standup": "standup_brief", "recap": "meeting_recap", "mine": "my_actions"}[
            command[1].lower()
        ]
        return Route(intent, date_expr)
    if _WHY.search(question):
        return Route("open_question", date_expr)  # reasoning over transcripts, even if it mentions a standup

    mentioned = [p for p in _mentioned_people(question, people) if p.id != asker_id]
    person = mentioned[0] if len(mentioned) == 1 else None
    candidates: list[str] = []
    if _MY_ACTIONS.search(question):
        candidates.append("my_actions")
    standup_word = bool(_STANDUP.search(question))
    recent = bool(date_expr or re.search(r"(last|latest|previous)", question, re.I))
    if _EVERYONE.search(question) or (standup_word and recent and person is None):
        candidates.append("standup_brief")
    if _RECAP.search(question):
        candidates.append("meeting_recap")
    if person is not None and _PERSON_VERB.search(question):
        candidates.append("person_recent")

    if len(candidates) == 1:
        return Route(candidates[0], date_expr, person)
    if candidates or standup_word or mentioned:
        return Route("", date_expr, person, candidates=candidates)
    return Route("open_question", date_expr)


ROUTER_SYSTEM = (
    "Classify a question someone asked a meeting-memory assistant. Intents: "
    "standup_brief (what the team did/said in a recent standup), person_recent (what one named person is working "
    "on or said recently), my_actions (the asker's own action items), meeting_recap (summary of a recent meeting), "
    "open_question (anything else: why/how/decisions/topics/searching). Return JSON only."
)
ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "person": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["intent", "person"],
    "additionalProperties": False,
}


async def route(
    question: str,
    people: list[PersonRef],
    asker_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    allow_llm: bool = True,
) -> Route:
    ruled = route_by_rules(question, people, asker_id)
    if ruled.intent:
        return ruled
    if not allow_llm:
        return Route("open_question", ruled.date_expression)
    response = await llm.create(
        "router",
        team_id=team_id,
        model=get_settings().router_model,
        max_tokens=100,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": question}],
        output_config={"format": {"type": "json_schema", "schema": ROUTER_SCHEMA}},
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
        intent = data["intent"] if data["intent"] in INTENTS else "open_question"
    except (json.JSONDecodeError, KeyError, TypeError):
        return Route("open_question", ruled.date_expression, source="haiku")
    person = ruled.person
    if intent == "person_recent" and data.get("person"):
        from recall.pipeline.speakers import SpeakerMatcher

        pid = SpeakerMatcher(people).match(str(data["person"]))
        person = next((p for p in people if p.id == pid), person)
        if person is None:
            intent = "open_question"
    return Route(intent, ruled.date_expression, person, source="haiku")
