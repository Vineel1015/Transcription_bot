"""ORM models. The schema is created by alembic/versions/0001_initial.py; tests/integration/test_schema.py
checks that these models and the migration stay in sync."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from recall.config import get_settings

EMBEDDING_DIM = get_settings().embedding_dim

MEETING_KINDS = ("standup", "planning", "retro", "sync", "other")
MEETING_SOURCES = ("teams", "upload")
MEETING_STATUSES = ("queued", "summarized", "indexed", "failed")
ACTION_STATUSES = ("open", "done", "dropped")
JOB_STATUSES = ("queued", "running", "done", "failed")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _created() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text, unique=True)
    service_url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    standup_time: Mapped[dt.time | None] = mapped_column(Time)
    holidays: Mapped[list[dt.date]] = mapped_column(ARRAY(Date), nullable=False, server_default="{}")
    title_patterns: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    join_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="25")
    budget_alert_month: Mapped[str | None] = mapped_column(Text)
    retention_media_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    retention_transcript_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="180")
    retention_recap_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="730")
    created_at: Mapped[dt.datetime] = _created()


class Person(Base):
    __tablename__ = "people"
    id: Mapped[uuid.UUID] = _uuid_pk()
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    entra_id: Mapped[str | None] = mapped_column(Text, unique=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[dt.datetime] = _created()


class TeamMember(Base):
    __tablename__ = "team_members"
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[dt.datetime] = _created()


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_meetings_source_external"),
        CheckConstraint(_in("kind", MEETING_KINDS), name="ck_meetings_kind"),
        CheckConstraint(_in("source", MEETING_SOURCES), name="ck_meetings_source"),
        CheckConstraint(_in("status", MEETING_STATUSES), name="ck_meetings_status"),
        Index("ix_meetings_team_started", "team_id", text("started_at DESC")),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="other")
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    media_path: Mapped[str | None] = mapped_column(Text)
    speaker_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    graph_participants: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    awaiting_batch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    batch_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created()


class Participant(Base):
    __tablename__ = "participants"
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[dt.datetime] = _created()


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("meeting_id", "seq", name="uq_segments_meeting_seq"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_label: Mapped[str] = mapped_column(Text, nullable=False)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _created()


class Recap(Base):
    __tablename__ = "recaps"
    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    overview: Mapped[str] = mapped_column(Text, nullable=False)
    topics: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    decisions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    open_questions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = _created()


class ActionItem(Base):
    __tablename__ = "action_items"
    __table_args__ = (
        CheckConstraint(_in("status", ACTION_STATUSES), name="ck_action_items_status"),
        Index("ix_action_items_owner_status", "owner_id", "status"),
        Index("ix_action_items_meeting", "meeting_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    owner_text: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    cite_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    closed_in_meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonUpdate(Base):
    __tablename__ = "person_updates"
    __table_args__ = (Index("ix_person_updates_person_meeting", "person_id", "meeting_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    speaker_label: Mapped[str] = mapped_column(Text, nullable=False)
    done: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    next: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    blockers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    cite_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = _created()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_meeting", "meeting_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="segment_window")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = _created()


class Brief(Base):
    __tablename__ = "briefs"
    __table_args__ = (UniqueConstraint("team_id", "for_date", name="uq_briefs_team_date"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    for_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rendered_card: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
        Index("ix_jobs_claim", "status", "run_after"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    run_after: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PeriodicRun(Base):
    __tablename__ = "periodic_runs"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class GraphSubscription(Base):
    __tablename__ = "graph_subscriptions"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = _created()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnswerCache(Base):
    __tablename__ = "answer_cache"
    __table_args__ = (UniqueConstraint("team_id", "question_hash", "date_scope", name="uq_answer_cache_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    question_hash: Mapped[str] = mapped_column(Text, nullable=False)
    date_scope: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = _created()


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_team_ts", "team_id", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    route: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("meetings.id", ondelete="SET NULL"))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    batch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
