"""Who may see what. A user sees a meeting if they attended it or belong to its team; admins see everything.
Every read in recall.query applies these filters."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from recall.db.models import Meeting, Participant, Person, TeamMember


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    person_id: uuid.UUID | None
    display_name: str = "service"
    is_admin: bool = False
    team_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @classmethod
    def service(cls) -> Principal:
        return cls(person_id=None, display_name="service", is_admin=True)


async def load_principal(session: AsyncSession, person: Person) -> Principal:
    team_ids = (
        await session.scalars(select(TeamMember.team_id).where(TeamMember.person_id == person.id))
    ).all()
    return Principal(
        person_id=person.id,
        display_name=person.display_name,
        is_admin=person.is_admin,
        team_ids=frozenset(team_ids),
    )


def visible_meetings(principal: Principal) -> ColumnElement[bool]:
    if principal.is_admin:
        return true()
    clauses: list[ColumnElement[bool]] = []
    if principal.team_ids:
        clauses.append(Meeting.team_id.in_(principal.team_ids))
    if principal.person_id is not None:
        attended = select(Participant.meeting_id).where(Participant.person_id == principal.person_id)
        clauses.append(Meeting.id.in_(attended))
    return or_(*clauses) if clauses else false()


def check_team(principal: Principal, team_id: uuid.UUID | None) -> None:
    if team_id is not None and not principal.is_admin and team_id not in principal.team_ids:
        raise Forbidden("not a member of this team")


def meeting_scope(principal: Principal, team_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Visibility filter, narrowed to one team when team_id is given (the caller must belong to it)."""
    if team_id is None:
        return visible_meetings(principal)
    check_team(principal, team_id)
    return Meeting.team_id == team_id
