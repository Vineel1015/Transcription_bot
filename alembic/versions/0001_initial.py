"""Initial schema.

Revision ID: 0001
Create Date: 2026-09-28

The DDL is frozen here (generated once from recall.db.models). tests/integration/test_schema.py fails if the
models drift from what migrations produce. The embedding dimension comes from EMBEDDING_DIM at migration time,
because it must match the embeddings provider (384 for the local model; 512 or 1024 for Voyage).
"""

from alembic import op

from recall.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE graph_subscriptions (
    id TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE jobs (
    id BIGSERIAL NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb NOT NULL,
    status TEXT DEFAULT 'queued' NOT NULL,
    attempts INTEGER DEFAULT '0' NOT NULL,
    max_attempts INTEGER DEFAULT '3' NOT NULL,
    run_after TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    locked_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    dedupe_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_jobs_status CHECK (status IN ('queued', 'running', 'done', 'failed')),
    UNIQUE (dedupe_key)
);

CREATE INDEX ix_jobs_claim ON jobs (status, run_after);

CREATE TABLE people (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    entra_id TEXT,
    aliases TEXT[] DEFAULT '{}' NOT NULL,
    is_admin BOOLEAN DEFAULT 'false' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (email),
    UNIQUE (entra_id)
);

CREATE TABLE periodic_runs (
    name TEXT NOT NULL,
    last_run_at TIMESTAMP WITH TIME ZONE NOT NULL,
    run_count INTEGER DEFAULT '0' NOT NULL,
    PRIMARY KEY (name)
);

CREATE TABLE teams (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    name TEXT NOT NULL,
    channel_id TEXT,
    service_url TEXT,
    timezone TEXT DEFAULT 'UTC' NOT NULL,
    standup_time TIME WITHOUT TIME ZONE,
    holidays DATE[] DEFAULT '{}' NOT NULL,
    title_patterns TEXT[] DEFAULT '{}' NOT NULL,
    join_urls TEXT[] DEFAULT '{}' NOT NULL,
    monthly_budget_usd NUMERIC(10, 2) DEFAULT '25' NOT NULL,
    budget_alert_month TEXT,
    retention_media_days INTEGER DEFAULT '30' NOT NULL,
    retention_transcript_days INTEGER DEFAULT '180' NOT NULL,
    retention_recap_days INTEGER DEFAULT '730' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (channel_id)
);

CREATE TABLE answer_cache (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    team_id UUID NOT NULL,
    question_hash TEXT NOT NULL,
    date_scope TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_answer_cache_key UNIQUE (team_id, question_hash, date_scope),
    FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE
);

CREATE TABLE audit_log (
    id BIGSERIAL NOT NULL,
    actor_id UUID,
    action TEXT NOT NULL,
    target TEXT,
    detail JSONB DEFAULT '{}'::jsonb NOT NULL,
    at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES people (id) ON DELETE SET NULL
);

CREATE TABLE briefs (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    team_id UUID NOT NULL,
    for_date DATE NOT NULL,
    content JSONB NOT NULL,
    rendered_card JSONB NOT NULL,
    posted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_briefs_team_date UNIQUE (team_id, for_date),
    FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE
);

CREATE TABLE meetings (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    team_id UUID,
    title TEXT NOT NULL,
    kind TEXT DEFAULT 'other' NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    ended_at TIMESTAMP WITH TIME ZONE,
    language TEXT,
    status TEXT DEFAULT 'queued' NOT NULL,
    error TEXT,
    media_path TEXT,
    speaker_overrides JSONB DEFAULT '{}'::jsonb NOT NULL,
    graph_participants JSONB DEFAULT '[]'::jsonb NOT NULL,
    awaiting_batch BOOLEAN DEFAULT 'false' NOT NULL,
    batch_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_meetings_source_external UNIQUE (source, external_id),
    CONSTRAINT ck_meetings_kind CHECK (kind IN ('standup', 'planning', 'retro', 'sync', 'other')),
    CONSTRAINT ck_meetings_source CHECK (source IN ('teams', 'upload')),
    CONSTRAINT ck_meetings_status CHECK (status IN ('queued', 'summarized', 'indexed', 'failed')),
    FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE
);

CREATE INDEX ix_meetings_team_started ON meetings (team_id, started_at DESC);

CREATE TABLE team_members (
    team_id UUID NOT NULL,
    person_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (team_id, person_id),
    FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE,
    FOREIGN KEY(person_id) REFERENCES people (id) ON DELETE CASCADE
);

CREATE TABLE action_items (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    meeting_id UUID NOT NULL,
    description TEXT NOT NULL,
    owner_id UUID,
    owner_text TEXT,
    due_date DATE,
    status TEXT DEFAULT 'open' NOT NULL,
    cite_ms INTEGER DEFAULT '0' NOT NULL,
    closed_in_meeting_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_action_items_status CHECK (status IN ('open', 'done', 'dropped')),
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE,
    FOREIGN KEY(owner_id) REFERENCES people (id) ON DELETE SET NULL,
    FOREIGN KEY(closed_in_meeting_id) REFERENCES meetings (id) ON DELETE SET NULL
);

CREATE INDEX ix_action_items_meeting ON action_items (meeting_id);

CREATE INDEX ix_action_items_owner_status ON action_items (owner_id, status);

CREATE TABLE chunks (
    id BIGSERIAL NOT NULL,
    meeting_id UUID NOT NULL,
    kind TEXT DEFAULT 'segment_window' NOT NULL,
    text TEXT NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    embedding VECTOR({dim}) NOT NULL,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE
);

CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX ix_chunks_meeting ON chunks (meeting_id);

CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv);

CREATE TABLE llm_usage (
    id BIGSERIAL NOT NULL,
    ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    route TEXT NOT NULL,
    model TEXT NOT NULL,
    team_id UUID,
    meeting_id UUID,
    input_tokens INTEGER DEFAULT '0' NOT NULL,
    cache_read_tokens INTEGER DEFAULT '0' NOT NULL,
    cache_write_tokens INTEGER DEFAULT '0' NOT NULL,
    output_tokens INTEGER DEFAULT '0' NOT NULL,
    batch BOOLEAN DEFAULT 'false' NOT NULL,
    cost_usd NUMERIC(12, 6) DEFAULT '0' NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE SET NULL,
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE SET NULL
);

CREATE INDEX ix_llm_usage_team_ts ON llm_usage (team_id, ts);

CREATE TABLE participants (
    meeting_id UUID NOT NULL,
    person_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (meeting_id, person_id),
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE,
    FOREIGN KEY(person_id) REFERENCES people (id) ON DELETE CASCADE
);

CREATE TABLE person_updates (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    meeting_id UUID NOT NULL,
    person_id UUID,
    speaker_label TEXT NOT NULL,
    done TEXT[] DEFAULT '{}' NOT NULL,
    next TEXT[] DEFAULT '{}' NOT NULL,
    blockers TEXT[] DEFAULT '{}' NOT NULL,
    cite_ms INTEGER DEFAULT '0' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE,
    FOREIGN KEY(person_id) REFERENCES people (id) ON DELETE SET NULL
);

CREATE INDEX ix_person_updates_person_meeting ON person_updates (person_id, meeting_id);

CREATE TABLE recaps (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    meeting_id UUID NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    overview TEXT NOT NULL,
    topics JSONB NOT NULL,
    decisions JSONB NOT NULL,
    open_questions JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (meeting_id),
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE
);

CREATE TABLE segments (
    id BIGSERIAL NOT NULL,
    meeting_id UUID NOT NULL,
    seq INTEGER NOT NULL,
    speaker_label TEXT NOT NULL,
    person_id UUID,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_segments_meeting_seq UNIQUE (meeting_id, seq),
    FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE,
    FOREIGN KEY(person_id) REFERENCES people (id) ON DELETE SET NULL
);
"""

TABLES = [
    "segments",
    "recaps",
    "person_updates",
    "participants",
    "llm_usage",
    "chunks",
    "action_items",
    "team_members",
    "meetings",
    "briefs",
    "audit_log",
    "answer_cache",
    "teams",
    "periodic_runs",
    "people",
    "jobs",
    "graph_subscriptions",
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for statement in DDL.replace("{dim}", str(get_settings().embedding_dim)).split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
