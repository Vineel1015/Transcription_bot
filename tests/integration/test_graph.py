from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest
import respx

from recall.config import get_settings
from recall.db.models import GraphSubscription, Job, Meeting, Participant
from recall.ingestion import graph
from recall.jobs import drain
from tests.conftest import FIXTURES, load_recap, recap_handler
from tests.fakes import FakeAnthropic
from tests.helpers import api_client, get

G = FIXTURES / "graph"
GRAPH = "https://graph.microsoft.com/v1.0"


def load(name: str) -> Any:
    return json.loads((G / name).read_text())


@pytest.fixture
def graph_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    s = get_settings()
    monkeypatch.setattr(s, "graph_tenant_id", "tenant")
    monkeypatch.setattr(s, "graph_client_id", "client")
    monkeypatch.setattr(s, "graph_client_secret", "secret")
    graph.set_graph(None)
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://login.microsoftonline.com/tenant/oauth2/v2.0/token").respond(
            json={"access_token": "graph-token", "expires_in": 3600}
        )
        yield mock
    graph.set_graph(None)


def test_parse_resource_forms() -> None:
    note = load("notification_created.json")["value"][0]
    org, mid, tid = graph.parse_resource(note["resource"]) or ("", "", "")
    assert org == "976f4b31-fd01-4e0b-9178-29cc40c14438"
    assert tid.startswith("MSMjMCMj")
    # tenant-level form without the user segment: organizer recovered from the meeting id
    assert graph.parse_resource(f"communications/onlineMeetings('{mid}')/transcripts('{tid}')") == (
        org,
        mid,
        tid,
    )
    assert graph.organizer_from_meeting_id(mid) == org


async def test_validation_handshake() -> None:
    async with api_client() as client:
        resp = await client.post("/graph/notifications?validationToken=abc%20123")
    assert resp.status_code == 200 and resp.text == "abc 123"
    assert resp.headers["content-type"].startswith("text/plain")


async def test_webhook_to_pipeline(
    graph_env: respx.MockRouter, fake_claude: FakeAnthropic, team_and_people: dict[str, Any]
) -> None:
    note = load("notification_created.json")
    org, mid, tid = graph.parse_resource(note["value"][0]["resource"]) or ("", "", "")
    base = f"{GRAPH}/users/{org}/onlineMeetings/{mid}"
    graph_env.get(base).respond(json=load("online_meeting.json"))
    graph_env.get(f"{base}/transcripts/{tid}").respond(json=load("transcript.json"))
    content = graph_env.get(f"{base}/transcripts/{tid}/content").respond(
        text=(FIXTURES / "standup_2026-09-25.vtt").read_text(), headers={"content-type": "text/vtt"}
    )
    fake_claude.handler = recap_handler(load_recap())

    async with api_client() as client:
        resp = await client.post("/graph/notifications", json=note)
        assert resp.status_code == 202
        again = await client.post("/graph/notifications", json=note)  # Graph retries: deduplicated
        assert again.status_code == 202
    assert len(await get(Job, kind="graph_ingest_transcript")) == 1

    await drain()
    assert content.calls.last.request.url.params["$format"] == "text/vtt"
    [meeting] = await get(Meeting, external_id=tid)
    assert meeting.team_id == team_and_people["team_id"]  # mapped by title pattern
    assert meeting.started_at == dt.datetime(
        2026, 9, 25, 9, 30, 4, tzinfo=dt.UTC
    )  # transcript time, not series
    assert meeting.status == "indexed"
    assert len(await get(Participant, meeting_id=meeting.id)) == 5
    assert graph_env.calls.call_count >= 4


async def test_bad_client_state_is_dropped(team_and_people: dict[str, Any]) -> None:
    note = load("notification_created.json")
    note["value"][0]["clientState"] = "forged"
    async with api_client() as client:
        resp = await client.post("/graph/notifications", json=note)
    assert resp.status_code == 202
    assert await get(Job) == []


async def test_unmapped_meeting_is_ignored(
    graph_env: respx.MockRouter, fake_claude: FakeAnthropic, team_and_people: dict[str, Any]
) -> None:
    meeting = load("online_meeting.json")
    meeting["subject"] = "Lunch and learn"
    meeting["joinWebUrl"] = "https://teams.microsoft.com/l/meetup-join/other"
    org, mid, tid = graph.parse_resource(load("notification_created.json")["value"][0]["resource"]) or (
        "",
        "",
        "",
    )
    base = f"{GRAPH}/users/{org}/onlineMeetings/{mid}"
    graph_env.get(base).respond(json=meeting)
    graph_env.get(f"{base}/transcripts/{tid}").respond(json=load("transcript.json"))
    graph_env.get(f"{base}/transcripts/{tid}/content").respond(text="WEBVTT\n")
    await graph.ingest_transcript({"organizer_id": org, "meeting_id": mid, "transcript_id": tid})
    assert await get(Meeting) == []


async def test_subscription_create_renew_and_lifecycle(graph_env: respx.MockRouter) -> None:
    create = graph_env.post(f"{GRAPH}/subscriptions").respond(json=load("subscription_created.json"))
    renew = graph_env.patch(f"{GRAPH}/subscriptions/0cc28c98-4d2a-4a34-b850-815d7e6486ea").respond(json={})
    now = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)

    await graph.ensure_subscription(now)
    body = json.loads(create.calls.last.request.content)
    assert body["resource"] == "communications/onlineMeetings/getAllTranscripts"
    assert body["lifecycleNotificationUrl"] == body["notificationUrl"]
    assert body["clientState"] == "test-client-state"
    [sub] = await get(GraphSubscription)

    await graph.ensure_subscription(now)  # far from expiry: no-op
    assert not renew.called
    await graph.ensure_subscription(sub.expires_at - dt.timedelta(hours=6))  # within 12 h: renew
    assert renew.called

    async with api_client() as client:
        resp = await client.post("/graph/notifications", json=load("notification_lifecycle.json"))
    assert resp.status_code == 202
    [job] = await get(Job, kind="graph_lifecycle")
    assert job.payload["event"] == "reauthorizationRequired"


async def test_poll_catches_missed_transcripts(
    graph_env: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = "976f4b31-fd01-4e0b-9178-29cc40c14438"
    monkeypatch.setattr(get_settings(), "graph_poll_organizers", org)
    route = graph_env.get(url__regex=rf"{GRAPH}/users/{org}/onlineMeetings/getAllTranscripts.*").respond(
        json=load("get_all_transcripts.json")
    )
    await graph.poll_transcripts()
    await graph.poll_transcripts()  # second poll finds the same transcript: deduplicated
    assert route.call_count == 2
    [job] = await get(Job, kind="graph_ingest_transcript")
    assert job.payload["organizer_id"] == org


def test_http_error_surfaces() -> None:
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/x")
    with pytest.raises(httpx.HTTPStatusError):
        httpx.Response(403, request=request).raise_for_status()
