from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import ActionItem, Meeting, Person
from recall.query.auth import Principal, meeting_scope
from recall.query.meetings import MeetingHeader, header_query, to_headers


@dataclass
class ActionRow:
    id: uuid.UUID
    description: str
    owner: str | None
    owner_id: uuid.UUID | None
    due_date: dt.date | None
    status: str
    cite_ms: int
    meeting: MeetingHeader

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "description": self.description,
            "owner": self.owner,
            "due": self.due_date.isoformat() if self.due_date else None,
            "status": self.status,
        }


async def get_action_items(
    session: AsyncSession,
    principal: Principal,
    *,
    status: str | None = "open",
    owner_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    since: dt.datetime | None = None,
    limit: int = 30,
) -> list[ActionRow]:
    stmt = (
        header_query()
        .add_columns(
            ActionItem.id,
            ActionItem.description,
            func.coalesce(Person.display_name, ActionItem.owner_text),
            ActionItem.owner_id,
            ActionItem.due_date,
            ActionItem.status,
            ActionItem.cite_ms,
        )
        .join(ActionItem, ActionItem.meeting_id == Meeting.id)
        .outerjoin(Person, Person.id == ActionItem.owner_id)
        .where(meeting_scope(principal, team_id))
    )
    if status:
        stmt = stmt.where(ActionItem.status == status)
    if owner_id:
        stmt = stmt.where(ActionItem.owner_id == owner_id)
    if since:
        stmt = stmt.where(Meeting.started_at >= since)
    stmt = stmt.order_by(ActionItem.due_date.asc().nulls_last(), Meeting.started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        ActionRow(
            id=row[8],
            description=row[9],
            owner=row[10],
            owner_id=row[11],
            due_date=row[12],
            status=row[13],
            cite_ms=row[14],
            meeting=to_headers([row[:8]])[0],
        )
        for row in rows
    ]


async def set_status(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID, status: str
) -> ActionItem | None:
    visible = select(Meeting.id).where(meeting_scope(principal, None))
    item = await session.scalar(
        select(ActionItem).where(ActionItem.id == item_id, ActionItem.meeting_id.in_(visible))
    )
    if item is None:
        return None
    item.status = status
    item.updated_at = dt.datetime.now(dt.UTC)
    return item
