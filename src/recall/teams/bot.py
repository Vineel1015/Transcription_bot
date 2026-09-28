"""Teams bot over the Bot Framework REST protocol: validates incoming activities, answers @mentions, 1:1 chats and
/standup, /recap, /mine, and posts proactive messages (standup briefs, budget alerts) to team channels."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from recall.agent.service import ask_stream
from recall.config import get_settings
from recall.db.models import Person, Team, TeamMember
from recall.db.session import session_scope
from recall.domain import Answer
from recall.jobs import handler
from recall.query.auth import Principal, load_principal
from recall.query.meetings import get_excerpt
from recall.teams import cards

log = logging.getLogger(__name__)

OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_ISSUER = "https://api.botframework.com"
UPDATE_EVERY_SECONDS = 1.5
_MENTION = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)


class BotAuthError(Exception):
    pass


class BotConnector:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=30)
        self._token: str | None = None
        self._expires = 0.0
        self._jwks: jwt.PyJWKClient | None = None

    # ---- inbound auth ----
    async def verify(self, authorization: str | None, service_url: str | None) -> None:
        s = get_settings()
        if s.dev_auth and not s.bot_enabled:
            return
        if not authorization or not authorization.lower().startswith("bearer "):
            raise BotAuthError("missing bearer token")
        token = authorization.split(" ", 1)[1]
        if self._jwks is None:
            config = (await self._http.get(OPENID_CONFIG)).json()
            self._jwks = jwt.PyJWKClient(config["jwks_uri"], cache_keys=True)
        try:
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=s.bot_app_id, issuer=BOT_ISSUER, leeway=300
            )
        except jwt.PyJWTError as exc:
            raise BotAuthError(str(exc)) from exc
        if (
            service_url
            and claims.get("serviceurl")
            and claims["serviceurl"].rstrip("/") != service_url.rstrip("/")
        ):
            raise BotAuthError("serviceUrl mismatch")

    # ---- outbound ----
    async def _bearer(self) -> str:
        s = get_settings()
        if self._token and time.time() < self._expires - 120:
            return self._token
        tenant = s.bot_tenant_id or "botframework.com"
        resp = await self._http.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": s.bot_app_id,
                "client_secret": s.bot_app_password,
                "scope": "https://api.botframework.com/.default",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    async def _call(self, method: str, url: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.request(
            method, url, json=body, headers={"Authorization": f"Bearer {await self._bearer()}"}
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def reply(self, activity: dict[str, Any], card: dict[str, Any], text: str = "") -> str | None:
        base = activity["serviceUrl"].rstrip("/")
        conv = activity["conversation"]["id"]
        out = _card_activity(card, text) | {"replyToId": activity.get("id")}
        data = await self._call(
            "POST", f"{base}/v3/conversations/{conv}/activities/{activity.get('id')}", out
        )
        return data.get("id")

    async def update(
        self, activity: dict[str, Any], activity_id: str, card: dict[str, Any], text: str = ""
    ) -> None:
        base = activity["serviceUrl"].rstrip("/")
        conv = activity["conversation"]["id"]
        await self._call(
            "PUT",
            f"{base}/v3/conversations/{conv}/activities/{activity_id}",
            _card_activity(card, text) | {"id": activity_id},
        )

    async def post_to_channel(
        self, service_url: str, channel_id: str, card: dict[str, Any], text: str = ""
    ) -> None:
        s = get_settings()
        body = {
            "isGroup": True,
            "channelData": {"channel": {"id": channel_id}},
            "bot": {"id": f"28:{s.bot_app_id}"},
            "activity": _card_activity(card, text),
        }
        await self._call("POST", f"{service_url.rstrip('/')}/v3/conversations", body)


def _card_activity(card: dict[str, Any], text: str = "") -> dict[str, Any]:
    return {
        "type": "message",
        "text": text,
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }


_connector: BotConnector | None = None


def get_connector() -> BotConnector:
    global _connector
    if _connector is None:
        _connector = BotConnector()
    return _connector


def set_connector(connector: BotConnector | None) -> None:
    global _connector
    _connector = connector


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", _MENTION.sub("", text or "")).strip()


async def _identify(activity: dict[str, Any]) -> tuple[Principal, uuid.UUID | None]:
    """Resolve the caller from aadObjectId, provisioning them (and their channel's team membership) on first
    contact so end users need no setup."""
    sender = activity.get("from") or {}
    aad = sender.get("aadObjectId")
    channel_id = ((activity.get("channelData") or {}).get("channel") or {}).get("id")
    async with session_scope() as session:
        team = await session.scalar(select(Team).where(Team.channel_id == channel_id)) if channel_id else None
        if team is not None and team.service_url != activity.get("serviceUrl"):
            team.service_url = activity.get("serviceUrl")
        person = await session.scalar(select(Person).where(Person.entra_id == aad)) if aad else None
        if person is None and aad:
            person = Person(display_name=sender.get("name") or "Unknown", entra_id=aad)
            session.add(person)
            await session.flush()
        if person is None:
            return Principal(person_id=None, display_name=sender.get("name") or "Unknown"), None
        if team is not None:
            await session.execute(
                insert(TeamMember).values(team_id=team.id, person_id=person.id).on_conflict_do_nothing()
            )
            await session.flush()
        principal = await load_principal(session, person)
        return principal, team.id if team else None


async def handle_activity(activity: dict[str, Any]) -> None:
    if activity.get("type") != "message":
        return
    connector = get_connector()
    principal, team_id = await _identify(activity)
    value = activity.get("value") or {}
    if value.get("action") == "excerpt":
        await _send_excerpt(activity, principal, value)
        return
    question = clean_text(activity.get("text", ""))
    if not question:
        await connector.reply(
            activity,
            cards.message_card(
                "Ask me about your meetings — for example “what did we cover in yesterday's standup?”, "
                "/standup, /recap or /mine."
            ),
        )
        return
    reply_id = await connector.reply(activity, cards.thinking_card())
    answer: Answer | None = None
    partial, last_update = "", time.monotonic()
    try:
        async for event in ask_stream(question, principal, team_id):
            if event.type == "delta":
                partial += event.text
                if reply_id and time.monotonic() - last_update > UPDATE_EVERY_SECONDS:
                    await connector.update(activity, reply_id, cards.message_card(partial + " …"))
                    last_update = time.monotonic()
            elif event.type == "reset":
                partial = ""
            elif event.type == "answer":
                answer = event.answer
    except Exception:
        log.exception("answering a Teams message failed")
        answer = Answer(
            answer="Sorry — something went wrong answering that. Please try again.", intent="error"
        )
    assert answer is not None
    card = answer.card or cards.answer_card(answer)
    if reply_id:
        await connector.update(activity, reply_id, card)
    else:
        await connector.reply(activity, card)


async def _send_excerpt(activity: dict[str, Any], principal: Principal, value: dict[str, Any]) -> None:
    start = int(value.get("start_ms") or 0)
    async with session_scope() as session:
        excerpt = await get_excerpt(
            session, principal, uuid.UUID(value["meeting_id"]), max(start - 15_000, 0), start + 90_000
        )
    if excerpt is None:
        await get_connector().reply(activity, cards.message_card("That meeting isn't available to you."))
        return
    meeting = excerpt["meeting"]
    await get_connector().reply(
        activity, cards.excerpt_card(meeting.title, meeting.cite(start).label, excerpt["segments"])
    )


async def post_to_team(team: Team, card: dict[str, Any], text: str = "") -> bool:
    s = get_settings()
    if not (s.bot_enabled and team.channel_id):
        return False
    await get_connector().post_to_channel(
        team.service_url or s.bot_default_service_url, team.channel_id, card, text
    )
    return True


@handler("budget_alert")
async def budget_alert(payload: dict[str, Any]) -> None:
    async with session_scope() as session:
        team = await session.get(Team, uuid.UUID(payload["team_id"]))
    if team is None:
        return
    text = (
        f"Recall has used ${float(payload['spend']):.2f} of this team's ${float(payload['budget']):.2f} monthly "
        "AI budget (80%). At 100%, open-ended questions switch to search-only answers; recaps continue. "
        "An admin can raise the budget with PATCH /v1/admin/teams/{id}."
    )
    await post_to_team(team, cards.message_card(text))
