from __future__ import annotations

import datetime as dt
from typing import Any

from recall.db.models import (
    ActionItem,
    Brief,
    Chunk,
    LlmUsage,
    Meeting,
    Participant,
    PersonUpdate,
    Recap,
    Segment,
)
from recall.jobs import drain
from recall.pipeline.run import batch_collect, batch_submit
from tests.conftest import load_recap, recap_handler
from tests.fakes import FakeAnthropic, json_message
from tests.helpers import DEV, add_prior_open_item, api_client, get, ingest_fixture


async def test_standup_end_to_end(fake_claude: FakeAnthropic, team_and_people: dict[str, Any]) -> None:
    team_id = team_and_people["team_id"]
    prior = await add_prior_open_item(team_id, team_and_people["priya"])
    fake_claude.handler = recap_handler(load_recap(str(prior)))

    meeting_id = await ingest_fixture(team_id)

    [meeting] = await get(Meeting, id=meeting_id)
    assert meeting.status == "indexed", meeting.error
    assert meeting.kind == "standup"
    # short standup -> Haiku, real-time, one call, structured output, cached system prompt
    [call] = fake_claude.calls
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "effort" not in call["output_config"]  # Haiku doesn't take effort
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = call["messages"][0]["content"]
    assert content.index("<transcript>") < content.index("Write the recap")  # transcript before instructions
    assert f"id={prior}" in content  # open items passed in for reconciliation

    segments = await get(Segment, meeting_id=meeting_id)
    assert all(s.person_id is not None for s in segments)  # every speaker matched
    assert "Um" not in " ".join(s.text for s in segments).split()
    assert len(await get(Participant, meeting_id=meeting_id)) == 5

    [recap] = await get(Recap, meeting_id=meeting_id)
    assert "Redis" in recap.decisions[0]["decision"]
    items = {i.description: i for i in await get(ActionItem, meeting_id=meeting_id)}
    runbook = items["Write the rollback runbook for the billing database"]
    assert runbook.owner_id == team_and_people["priya"]
    assert runbook.due_date == dt.date(2026, 9, 30)
    updates = await get(PersonUpdate, meeting_id=meeting_id)
    assert {u.person_id for u in updates} == {
        team_and_people[k] for k in ("priya", "marcus", "aisha", "diego", "hannah")
    }

    [closed] = await get(ActionItem, id=prior)
    assert closed.status == "done" and closed.closed_in_meeting_id == meeting_id

    chunks = await get(Chunk, meeting_id=meeting_id)
    assert {c.kind for c in chunks} == {"segment_window", "recap_section"}
    [brief] = await get(Brief, team_id=team_id)
    assert brief.for_date == dt.date(2026, 9, 28)  # Friday's meeting feeds Monday's brief
    assert len(brief.content["people"]) == 5

    [usage] = await get(LlmUsage, meeting_id=meeting_id)
    assert usage.route == "summarize" and usage.cost_usd > 0 and usage.batch is False


async def test_short_meeting_is_skipped_before_any_model_call(
    fake_claude: FakeAnthropic, team_and_people: dict[str, Any]
) -> None:
    async with api_client() as client:
        resp = await client.post(
            "/v1/upload",
            headers=DEV,
            files={"file": ("quick.txt", b"[00:00:01] Priya Shah: quick one\n[00:00:20] Marcus Chen: ok\n")},
            data={
                "team_id": str(team_and_people["team_id"]),
                "title": "Quick sync",
                "started_at": "2026-09-25T15:00:00Z",
            },
        )
    assert resp.status_code == 202, resp.text
    await drain()
    [meeting] = await get(Meeting, title="Quick sync")
    assert meeting.status == "indexed" and meeting.error and meeting.error.startswith("skipped:")
    assert fake_claude.calls == []


async def test_non_standup_goes_through_batch_api(
    fake_claude: FakeAnthropic, team_and_people: dict[str, Any]
) -> None:
    recap = load_recap()
    recap["kind"] = "planning"
    fake_claude.handler = lambda params: json_message(recap)
    meeting_id = await ingest_fixture(
        team_and_people["team_id"],
        "planning_meeting.vtt",
        "Platform Planning",
        dt.datetime(2026, 9, 25, 15, 0, tzinfo=dt.UTC),
    )
    [meeting] = await get(Meeting, id=meeting_id)
    assert meeting.awaiting_batch and meeting.status == "queued"
    assert fake_claude.calls == []  # nothing sent in real time

    await batch_submit()
    [meeting] = await get(Meeting, id=meeting_id)
    assert meeting.batch_id in fake_claude.messages.batches.batches
    await batch_collect()  # batch not ended yet: nothing happens
    assert (await get(Meeting, id=meeting_id))[0].status == "queued"

    fake_claude.messages.batches.ended.add(meeting.batch_id)
    await batch_collect()
    [meeting] = await get(Meeting, id=meeting_id)
    assert meeting.status == "indexed" and not meeting.awaiting_batch
    [call] = fake_claude.calls
    assert call["model"] == "claude-sonnet-5" and call["output_config"]["effort"] == "medium"
    [usage] = await get(LlmUsage, meeting_id=meeting_id)
    assert usage.batch is True


async def test_upload_media_then_map_speakers(
    fake_claude: FakeAnthropic, team_and_people: dict[str, Any], monkeypatch: Any
) -> None:
    from recall.domain import Cue
    from recall.ingestion import stt

    words = " ".join(["update"] * 40)
    cues = [
        Cue(
            speaker=f"Speaker {i % 2 + 1}",
            start_ms=i * 20_000,
            end_ms=i * 20_000 + 19_000,
            text=f"{words} {i}",
        )
        for i in range(12)
    ]

    async def fake_transcribe(data: bytes, filename: str) -> list[Cue]:
        return cues

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    fake_claude.handler = recap_handler(load_recap())
    async with api_client() as client:
        resp = await client.post(
            "/v1/upload",
            headers=DEV,
            files={"file": ("standup.m4a", b"\x00fake-audio")},
            data={
                "team_id": str(team_and_people["team_id"]),
                "title": "Platform Standup",
                "started_at": "2026-09-25T13:30:00Z",
            },
        )
        assert resp.status_code == 202
        meeting_id = resp.json()["meeting_id"]
        await drain()
        assert all(s.person_id is None for s in await get(Segment))  # diarized labels don't match anyone
        resp = await client.patch(
            f"/v1/meetings/{meeting_id}/speakers",
            headers=DEV,
            json={"Speaker 1": str(team_and_people["priya"]), "Speaker 2": str(team_and_people["marcus"])},
        )
        assert resp.status_code == 200
    calls_before = len(fake_claude.calls)
    await drain()
    assert len(fake_claude.calls) == calls_before + 1  # extraction re-ran; STT did not
    segments = await get(Segment)
    assert {s.person_id for s in segments} == {team_and_people["priya"], team_and_people["marcus"]}
