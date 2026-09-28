# Architecture

```
Teams Graph webhook ─┐                                        ┌─► Teams bot (@Recall, /recap, /standup, /mine)
Graph poll (15 min) ─┼─► jobs table ─► pipeline ─► Postgres ◄─┼─► REST  POST /v1/ask (SSE stream)
Upload endpoint ─────┘   (SKIP LOCKED)                         ├─► MCP server (recall-mcp)
                                                               └─► Standup brief scheduler
   normalize → match speakers → summarize → persist → embed → precompute briefs
```

There are two containers: `app` (FastAPI, job workers and scheduler in one process) and `postgres` (pgvector).
Postgres is the only datastore and also the job queue.

## Code map

| Path | Role |
|---|---|
| `ingestion/graph.py` | Graph change notifications, subscription create/renew, 15-minute poll, transcript download |
| `api/routes.py` (`/v1/upload`) | Uploaded transcripts (.vtt/.txt/.docx) or media (→ speech-to-text) |
| `ingestion/vtt.py`, `stt.py`, `storage.py` | Parsers, Deepgram / faster-whisper, local / Azure Blob / S3 media |
| `pipeline/run.py` | Pipeline stages, job handlers, Batch API submit and collect |
| `pipeline/summarize.py` | Summarizer prompt, model routing, structured output, map-reduce for very long meetings |
| `pipeline/speakers.py` | Label → person matching: override → Entra ID → email → name/alias → fuzzy (≥ 0.9) → unique first name |
| `pipeline/embed.py` | Embedding providers (local fastembed, Voyage, hash) and chunking |
| `pipeline/briefs.py` | Precomputed standup briefs; clears the answer cache |
| `query/` | **The only read API.** Authorization lives here (`auth.py`); dates in `dates.py` |
| `agent/router.py` | Rules first; Haiku only when the rules are ambiguous |
| `agent/fastpath.py` | Answers served with no model call |
| `agent/agent.py`, `tools.py` | Retrieval Agent: streaming tool loop, capped tool calls, `[m2@252000]` citation refs |
| `agent/service.py` | `ask()`: cache → router → fast path → budget → agent → cache |
| `teams/bot.py`, `cards.py` | Bot Framework REST protocol, Adaptive Cards, proactive posts |
| `jobs.py` | Queue (SKIP LOCKED), worker with graceful shutdown, singleton periodic tasks (advisory locks) |
| `scheduler.py` | Pre-standup brief, retention purge, `ops_metrics` for alerts |

The Retrieval Agent never builds SQL. It calls typed tools, and each tool wraps a `query/` function that applies
the caller's permissions.

## Answering a question

1. **Answer cache.** Key: normalized question + resolved date scope, per team. A newly indexed meeting clears the
   team's entries. A hit makes no model call.
2. **Router.** Regex/keyword rules pick `standup_brief`, `person_recent`, `my_actions`, `meeting_recap` or
   `open_question`. One Haiku call is made only when the rules are ambiguous, and never when the team is over budget.
3. **Fast path.** The first four intents are answered from `briefs`, `person_updates`, `action_items` and
   `recaps` with templated cards. There's no model call; the bench measures about 25 ms.
4. **Budget.** Over 100%, open questions get search-only answers (top passages with citations).
5. **Agent.**
   - Model: `claude-sonnet-5` at `effort: low`, streaming, at most 4 tool calls. After the 4th, `tool_choice` is `none`.
   - Caching: the system prompt and tools are byte-stable and prompt-cached; volatile content (date, question)
     comes after the breakpoint.
   - Escalation: if no tool found anything, it retries once at `medium` effort.
   - Only answers backed by tool results are cached.

## Cost controls

| Control | Where |
|---|---|
| Standups ≤ 30 min summarized by Haiku 4.5; others by Sonnet 5 at medium effort | `summarize.choose_model` |
| Non-standup meetings go through the Message Batches API (50% off), within 1 hour | `pipeline/run.py: batch_submit / batch_collect` |
| Meetings < 3 min or < 300 words skipped before any model call | `run.skip_reason` |
| Summarize once; the agent reads recaps before raw excerpts | agent prompt, tool design |
| Compact tool results: k=5 search, 300-char snippets, ≤ 3-minute excerpts, ≤ 30 open items | `agent/tools.py`, `summarize.py` |
| Every call logged with cost | `llm_usage`, via `llm.create` / `record_usage` |
| Team monthly budget: alert at 80%; at 100% search-only (recaps continue) | `llm._maybe_alert_budget`, `service._search_only` |
| Spend report | `GET /v1/admin/usage` |

Prices live in `config.DEFAULT_PRICES` (USD per million tokens). Override them with `LLM_PRICES_JSON`.

## Data model

| Group | Tables |
|---|---|
| Organization | `teams` (timezone, standup time, holidays, budget, retention, title patterns / join URLs for mapping), `people`, `team_members` |
| Raw | `meetings` (status: queued → summarized → indexed / failed), `participants`, `segments` |
| Extracted | `recaps`, `action_items` (tracked across meetings; `closed_in_meeting_id`), `person_updates` |
| Speed | `chunks` (HNSW cosine + GIN full-text), `briefs`, `answer_cache` |
| Operations | `jobs`, `periodic_runs`, `graph_subscriptions`, `llm_usage`, `audit_log` |

The schema is created by `alembic/versions/0001_initial.py`. `tests/integration/test_jobs_and_schema.py` fails
if the ORM models drift from it.
