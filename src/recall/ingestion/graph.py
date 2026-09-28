"""Teams transcripts via Microsoft Graph: change notifications (primary), a 15-minute poll (catches missed
webhooks), and subscription renewal. App-only auth with client credentials.

Graph paths used (checked against Microsoft Learn, 2026-06):
  subscription resource   communications/onlineMeetings/getAllTranscripts   (changeType: created)
  notification resource   users/{organizerId}/onlineMeetings('{meetingId}')/transcripts('{transcriptId}')
  transcript metadata     GET /users/{org}/onlineMeetings/{meetingId}/transcripts/{transcriptId}
  transcript content      GET .../transcripts/{transcriptId}/content?$format=text/vtt
  meeting metadata        GET /users/{org}/onlineMeetings/{meetingId}
  poll                    GET /users/{org}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{org}',
                              startDateTime={iso})
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy import delete, select

from recall.config import get_settings
from recall.db.models import GraphSubscription, Team
from recall.db.session import session_scope
from recall.ingestion.vtt import parse_vtt
from recall.jobs import enqueue, handler, periodic

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SUBSCRIPTION_RESOURCE = "communications/onlineMeetings/getAllTranscripts"
_RESOURCE = re.compile(
    r"users/(?:\('?)?(?P<org>[^/'()]+)(?:'?\))?/onlineMeetings(?:\('|/)(?P<mid>[^'/()]+)(?:'\))?"
    r"/transcripts(?:\('|/)(?P<tid>[^'/()]+)",
    re.IGNORECASE,
)
_RESOURCE_NO_USER = re.compile(
    r"onlineMeetings(?:\('|/)(?P<mid>[^'/()]+)(?:'\))?/transcripts(?:\('|/)(?P<tid>[^'/()]+)", re.IGNORECASE
)


class GraphClient:
    def __init__(
        self, tenant_id: str, client_id: str, client_secret: str, http: httpx.AsyncClient | None = None
    ):
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self._http = http or httpx.AsyncClient(timeout=60)
        self._token: str | None = None
        self._expires = 0.0

    async def token(self) -> str:
        if self._token and time.time() < self._expires - 120:
            return self._token
        resp = await self._http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self.token()}", **kw.pop("headers", {})}
        resp = await self._http.request(method, f"{GRAPH}/{path.lstrip('/')}", headers=headers, **kw)
        resp.raise_for_status()
        return resp

    async def get_json(self, path: str, **kw: Any) -> dict[str, Any]:
        data: dict[str, Any] = (await self.request("GET", path, **kw)).json()
        return data

    async def get_meeting(self, org: str, meeting_id: str) -> dict[str, Any]:
        return await self.get_json(f"users/{org}/onlineMeetings/{meeting_id}")

    async def get_transcript(self, org: str, meeting_id: str, transcript_id: str) -> dict[str, Any]:
        return await self.get_json(f"users/{org}/onlineMeetings/{meeting_id}/transcripts/{transcript_id}")

    async def get_transcript_vtt(self, org: str, meeting_id: str, transcript_id: str) -> str:
        resp = await self.request(
            "GET",
            f"users/{org}/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content",
            params={"$format": "text/vtt"},
        )
        return resp.text

    async def list_transcripts(self, org: str, since: dt.datetime) -> list[dict[str, Any]]:
        stamp = since.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = (
            f"users/{org}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{org}',"
            f"startDateTime={stamp})"
        )
        items: list[dict[str, Any]] = []
        url: str | None = path
        while url:
            page = await self.get_json(url.replace(GRAPH, ""))
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
        return items

    async def create_subscription(
        self, notification_url: str, client_state: str, expires: dt.datetime
    ) -> dict[str, Any]:
        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            # required when the expiration is more than an hour away
            "lifecycleNotificationUrl": notification_url,
            "resource": SUBSCRIPTION_RESOURCE,
            "expirationDateTime": expires.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
            "clientState": client_state,
        }
        data: dict[str, Any] = (await self.request("POST", "subscriptions", json=body)).json()
        return data

    async def renew_subscription(self, subscription_id: str, expires: dt.datetime) -> dict[str, Any]:
        body = {"expirationDateTime": expires.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")}
        data: dict[str, Any] = (
            await self.request("PATCH", f"subscriptions/{subscription_id}", json=body)
        ).json()
        return data


_client: GraphClient | None = None


def get_graph() -> GraphClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = GraphClient(s.graph_tenant_id, s.graph_client_id, s.graph_client_secret)
    return _client


def set_graph(client: GraphClient | None) -> None:
    global _client
    _client = client


def organizer_from_meeting_id(meeting_id: str) -> str | None:
    """Teams online meeting ids are base64 of '1*{organizerId}*0**{threadId}'."""
    try:
        decoded = base64.b64decode(meeting_id + "=" * (-len(meeting_id) % 4)).decode("utf-8", "ignore")
    except ValueError:
        return None
    parts = decoded.split("*")
    return parts[1] if len(parts) > 2 and re.fullmatch(r"[0-9a-fA-F-]{36}", parts[1]) else None


def parse_resource(resource: str) -> tuple[str, str, str] | None:
    """-> (organizer_id, meeting_id, transcript_id)"""
    match = _RESOURCE.search(resource)
    if match:
        return match["org"], match["mid"], match["tid"]
    match = _RESOURCE_NO_USER.search(resource)
    if match:
        org = organizer_from_meeting_id(match["mid"])
        if org:
            return org, match["mid"], match["tid"]
    return None


async def handle_notifications(body: dict[str, Any]) -> int:
    """Validate and queue Graph change/lifecycle notifications. Returns the number of jobs queued."""
    s = get_settings()
    queued = 0
    async with session_scope() as session:
        for note in body.get("value", []):
            if note.get("clientState") != s.graph_client_state:
                log.warning("dropping Graph notification with a bad clientState")
                continue
            if note.get("lifecycleEvent"):
                job = await enqueue(
                    session,
                    "graph_lifecycle",
                    {"event": note["lifecycleEvent"], "subscription_id": note.get("subscriptionId")},
                )
                queued += job is not None
                continue
            parsed = parse_resource(note.get("resource") or note.get("resourceData", {}).get("@odata.id", ""))
            if parsed is None:
                log.warning("unrecognised Graph resource %r", note.get("resource"))
                continue
            org, mid, tid = parsed
            job = await enqueue(
                session,
                "graph_ingest_transcript",
                {"organizer_id": org, "meeting_id": mid, "transcript_id": tid},
                dedupe_key=f"graph:{tid}",
            )
            queued += job is not None
    return queued


def match_team(teams: list[Team], title: str, join_url: str | None) -> Team | None:
    for team in teams:
        if join_url and any(join_url.split("?")[0] == u.split("?")[0] for u in team.join_urls):
            return team
    for team in teams:
        for pattern in team.title_patterns:
            try:
                if re.search(pattern, title, re.IGNORECASE):
                    return team
            except re.error:
                if pattern.lower() in title.lower():
                    return team
    return None


def _participants(meeting: dict[str, Any]) -> list[dict[str, Any]]:
    parts = meeting.get("participants") or {}
    people = [parts.get("organizer") or {}, *(parts.get("attendees") or [])]
    out = []
    for p in people:
        user = (p.get("identity") or {}).get("user") or {}
        if user.get("id") or p.get("upn"):
            out.append({"entra_id": user.get("id"), "name": user.get("displayName"), "email": p.get("upn")})
    return out


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@handler("graph_ingest_transcript")
async def ingest_transcript(payload: dict[str, Any]) -> None:
    from recall.pipeline.run import create_meeting

    graph = get_graph()
    org, mid, tid = payload["organizer_id"], payload["meeting_id"], payload["transcript_id"]
    meeting = await graph.get_meeting(org, mid)
    transcript = await graph.get_transcript(org, mid, tid)
    vtt = await graph.get_transcript_vtt(org, mid, tid)
    title = meeting.get("subject") or "Teams meeting"
    # For recurring meetings the onlineMeeting times are the series'; the transcript's are this occurrence's.
    started = (
        _ts(transcript.get("createdDateTime")) or _ts(meeting.get("startDateTime")) or dt.datetime.now(dt.UTC)
    )
    ended = _ts(transcript.get("endDateTime")) or _ts(meeting.get("endDateTime"))
    async with session_scope() as session:
        teams = list((await session.scalars(select(Team))).all())
        team = match_team(teams, title, meeting.get("joinWebUrl"))
        if team is None:
            log.info("transcript %s (%s) doesn't map to a configured team; skipping", tid, title)
            return
        await create_meeting(
            session,
            title=title,
            source="teams",
            external_id=tid,
            team_id=team.id,
            started_at=started,
            ended_at=ended,
            cues=parse_vtt(vtt),
            graph_participants=_participants(meeting),
        )


@handler("graph_lifecycle")
async def lifecycle(payload: dict[str, Any]) -> None:
    event = payload.get("event")
    if event == "missed":
        await poll_transcripts()
    elif event in ("reauthorizationRequired", "subscriptionRemoved"):
        if event == "subscriptionRemoved":
            async with session_scope() as session:
                await session.execute(
                    delete(GraphSubscription).where(GraphSubscription.id == payload.get("subscription_id"))
                )
        await ensure_subscription()


async def ensure_subscription(now: dt.datetime | None = None) -> None:
    """Create the tenant-wide transcript subscription, or renew it when it expires within 12 hours."""
    s = get_settings()
    if not s.graph_enabled:
        return
    now = now or dt.datetime.now(dt.UTC)
    expires = now + dt.timedelta(hours=s.graph_subscription_hours)
    graph = get_graph()
    async with session_scope() as session:
        sub = await session.scalar(
            select(GraphSubscription).where(GraphSubscription.resource == SUBSCRIPTION_RESOURCE)
        )
        if sub is not None and sub.expires_at > now + dt.timedelta(hours=12):
            return
        if sub is not None:
            try:
                await graph.renew_subscription(sub.id, expires)
                sub.expires_at = expires
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                await session.delete(sub)
        created = await graph.create_subscription(s.webhook_url, s.graph_client_state, expires)
        session.add(
            GraphSubscription(
                id=created["id"],
                resource=SUBSCRIPTION_RESOURCE,
                expires_at=_ts(created.get("expirationDateTime")) or expires,
            )
        )


@periodic("graph_renew", dt.timedelta(hours=1))
async def renew() -> None:
    await ensure_subscription()


@periodic("graph_poll", dt.timedelta(minutes=15))
async def poll_transcripts() -> None:
    s = get_settings()
    organizers = [o.strip() for o in s.graph_poll_organizers.split(",") if o.strip()]
    if not s.graph_enabled or not organizers:
        return
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
    graph = get_graph()
    async with session_scope() as session:
        for org in organizers:
            for item in await graph.list_transcripts(org, since):
                mid = item.get("meetingId")
                if not mid:
                    continue
                await enqueue(
                    session,
                    "graph_ingest_transcript",
                    {"organizer_id": org, "meeting_id": mid, "transcript_id": item["id"]},
                    dedupe_key=f"graph:{item['id']}",
                )
