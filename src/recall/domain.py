"""Plain data types shared by ingestion, the pipeline and the query layer."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

MeetingKind = Literal["standup", "planning", "retro", "sync", "other"]


class Cue(BaseModel):
    speaker: str
    start_ms: int
    end_ms: int
    text: str


class SegmentIn(BaseModel):
    seq: int
    speaker_label: str
    start_ms: int
    end_ms: int
    text: str
    person_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------------------------------------
# Summarizer output. The JSON schema sent to Claude (structured outputs) is RECAP_JSON_SCHEMA below; this
# pydantic model validates what comes back.
# ---------------------------------------------------------------------------------------------------------


class Topic(BaseModel):
    title: str
    summary: str
    start_ms: int = 0


class Decision(BaseModel):
    decision: str
    rationale: str = ""
    start_ms: int = 0


class ActionItemOut(BaseModel):
    description: str
    owner: str | None = None
    due: dt.date | None = None
    start_ms: int = 0


class PersonUpdateOut(BaseModel):
    speaker: str
    done: list[str] = Field(default_factory=list)
    next: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    start_ms: int = 0


class RecapOut(BaseModel):
    overview: str
    kind: MeetingKind
    topics: list[Topic] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    action_items: list[ActionItemOut] = Field(default_factory=list)
    person_updates: list[PersonUpdateOut] = Field(default_factory=list)
    completed_prior_action_item_ids: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_STRS = {"type": "array", "items": _STR}

RECAP_JSON_SCHEMA: dict[str, Any] = _obj(
    {
        "overview": _STR,
        "kind": {"type": "string", "enum": ["standup", "planning", "retro", "sync", "other"]},
        "topics": {"type": "array", "items": _obj({"title": _STR, "summary": _STR, "start_ms": _INT})},
        "decisions": {
            "type": "array",
            "items": _obj({"decision": _STR, "rationale": _STR, "start_ms": _INT}),
        },
        "action_items": {
            "type": "array",
            "items": _obj(
                {
                    "description": _STR,
                    "owner": {"anyOf": [_STR, {"type": "null"}]},
                    "due": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
                    "start_ms": _INT,
                }
            ),
        },
        "person_updates": {
            "type": "array",
            "items": _obj(
                {"speaker": _STR, "done": _STRS, "next": _STRS, "blockers": _STRS, "start_ms": _INT}
            ),
        },
        "completed_prior_action_item_ids": _STRS,
        "open_questions": _STRS,
    }
)


class Citation(BaseModel):
    meeting_id: uuid.UUID
    title: str
    started_at: dt.datetime
    start_ms: int = 0
    label: str


class Answer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    intent: str = "open_question"
    card: dict[str, Any] | None = None
    cached: bool = False
