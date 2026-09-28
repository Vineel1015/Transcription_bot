from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from recall.config import get_settings
from recall.db.models import Brief, Person, TeamMember
from recall.scheduler import brief_due, post_due_briefs
from recall.teams import bot
from tests.conftest import load_recap, recap_handler
from tests.fakes import FakeAnthropic
from tests.helpers import api_client, get, ingest_fixture


class FakeConnector(bot.BotConnector):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def verify(self, authorization: str | None, service_url: str | None) -> None:
        if authorization != "Bearer valid":
            raise bot.BotAuthError("bad token")

    async def reply(self, activity: dict[str, Any], card: dict[str, Any], text: str = "") -> str | None:
        self.sent.append(("reply", card))
        return f"act-{len(self.sent)}"

    async def update(
        self, activity: dict[str, Any], activity_id: str, card: dict[str, Any], text: str = ""
    ) -> None:
        self.sent.append(("update", card))

    async def post_to_channel(
        self, service_url: str, channel_id: str, card: dict[str, Any], text: str = ""
    ) -> None:
        self.sent.append(("channel", card))


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = FakeConnector()
    bot.set_connector(fake)
    monkeypatch.setattr(get_settings(), "bot_app_id", "bot-app")
    monkeypatch.setattr(get_settings(), "bot_app_password", "bot-secret")
    yield fake
    bot.set_connector(None)


def activity(text: str, aad: str, name: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "incoming-1",
        "text": text,
        "value": value,
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "from": {"id": "29:x", "aadObjectId": aad, "name": name},
        "conversation": {"id": "19:platform@thread.tacv2;messageid=1"},
        "channelData": {"channel": {"id": "19:platform@thread.tacv2"}, "team": {"id": "19:team"}},
    }


def card_text(card: dict[str, Any]) -> str:
    return " ".join(str(block.get("text", "")) for block in card["body"])


async def test_mention_gets_thinking_then_answer(
    connector: FakeConnector, fake_claude: FakeAnthropic, team_and_people: dict[str, Any]
) -> None:
    fake_claude.handler = recap_handler(load_recap())
    await ingest_fixture(team_and_people["team_id"])
    msg = activity("<at>Recall</at> /standup", "976f4b31-fd01-4e0b-9178-29cc40c14438", "Hannah Lee")
    await bot.handle_activity(msg)
    kinds = [k for k, _ in connector.sent]
    assert kinds[0] == "reply" and "Thinking" in card_text(connector.sent[0][1])
    assert kinds[-1] == "update"
    final = connector.sent[-1][1]
    assert "Standup brief" in card_text(final)
    excerpt_actions = [a for a in final["actions"] if a["data"]["action"] == "excerpt"]
    assert excerpt_actions  # citations are clickable

    connector.sent.clear()
    await bot.handle_activity(
        activity("", "976f4b31-fd01-4e0b-9178-29cc40c14438", "Hannah Lee", value=excerpt_actions[0]["data"])
    )
    [(kind, card)] = connector.sent
    assert kind == "reply" and "Priya Shah" in card_text(card)


async def test_unknown_user_is_provisioned_into_channel_team(
    connector: FakeConnector, team_and_people: dict[str, Any]
) -> None:
    await bot.handle_activity(activity("/mine", "55555555-5555-4555-8555-555555555555", "New Person"))
    [person] = await get(Person, entra_id="55555555-5555-4555-8555-555555555555")
    assert person.display_name == "New Person"
    assert await get(TeamMember, person_id=person.id)
    assert "no open action items" in card_text(connector.sent[-1][1])


async def test_messages_endpoint_rejects_bad_tokens(connector: FakeConnector) -> None:
    async with api_client() as client:
        bad = await client.post(
            "/api/messages", json=activity("hi", "x", "X"), headers={"Authorization": "Bearer forged"}
        )
        good = await client.post(
            "/api/messages", json={"type": "conversationUpdate"}, headers={"Authorization": "Bearer valid"}
        )
    assert bad.status_code == 401 and good.status_code == 200


async def test_pre_standup_brief_posts_once(
    connector: FakeConnector, fake_claude: FakeAnthropic, team_and_people: dict[str, Any]
) -> None:
    fake_claude.handler = recap_handler(load_recap())
    await ingest_fixture(team_and_people["team_id"])
    before = dt.datetime(2026, 9, 28, 13, 22, tzinfo=dt.UTC)  # 09:22 New York, standup at 09:30
    too_early = dt.datetime(2026, 9, 28, 13, 15, tzinfo=dt.UTC)  # 09:15: outside the 10-minute window

    assert await post_due_briefs(too_early) == []
    assert await post_due_briefs(before) == ["Platform"]
    assert await post_due_briefs(before + dt.timedelta(minutes=1)) == []  # already posted
    [(kind, card)] = connector.sent
    assert kind == "channel" and "Fri 25 Sep" in card_text(card)
    [brief] = await get(Brief, for_date=dt.date(2026, 9, 28))
    assert brief.posted_at is not None


async def test_no_brief_when_nothing_recorded(
    connector: FakeConnector, team_and_people: dict[str, Any]
) -> None:
    assert await post_due_briefs(dt.datetime(2026, 9, 28, 13, 22, tzinfo=dt.UTC)) == []
    assert connector.sent == []


def test_brief_window_skips_weekends() -> None:
    from recall.db.models import Team

    team = Team(name="t", channel_id="c", timezone="UTC", standup_time=dt.time(9, 30), holidays=[])
    assert brief_due(team, dt.datetime(2026, 9, 28, 9, 25, tzinfo=dt.UTC)) == dt.date(2026, 9, 28)
    assert brief_due(team, dt.datetime(2026, 9, 26, 9, 25, tzinfo=dt.UTC)) is None  # Saturday
    assert brief_due(team, dt.datetime(2026, 9, 28, 9, 31, tzinfo=dt.UTC)) is None
