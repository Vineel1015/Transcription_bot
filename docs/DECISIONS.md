# Decisions and deviations

Choices made while building Recall from `IMPLEMENTATION_PROMPT.md`, including every place the build differs from the spec.

## Deviations from the spec

| Spec | Built | Why |
|---|---|---|
| Summarizer uses *forced tool use* with a `submit_recap` tool | **Structured outputs** (`output_config.format` with a JSON schema) | Same guarantee (schema-valid JSON), works with adaptive thinking on Sonnet 5, and the request params work unchanged in the Batch API. Forced `tool_choice` is rejected on newer Claude models. |
| `botbuilder-core` for the bot | **Bot Framework REST protocol over `httpx` + PyJWT** (`teams/bot.py`) | Avoids botbuilder's legacy dependency stack (msrest plus an aiohttp-bound adapter) next to FastAPI. The protocol is small: validate the inbound JWT, reply, update, and create a conversation. It's also fully testable with fakes. |
| `msal` for Graph auth | **Client-credentials token over `httpx`** | msal is synchronous, and its HTTP layer can't be mocked with `respx`. The token flow is a single POST. |
| Agent tools take `team_id` | **The server binds the team scope**; tools don't accept it | The model never sees team ids. A team-scoped question can only read that team's meetings, which is also what makes the shared answer cache safe. |
| Meeting status `queued → summarized → indexed / failed` | Same, plus a boolean `meetings.awaiting_batch` and `batch_id` | Distinguishes "waiting for the Batch API" from "not yet processed" without another status value. |
| `meeting_series` table (earlier spec draft) | Mapping lives on `teams` (`title_patterns`, `join_urls`, `standup_time`) | The final spec's data model has no series table. Recurring Teams meetings share one onlineMeeting id, so the **transcript id** is the dedupe key, and the transcript's timestamps (not the series') date each occurrence. |
| 15-minute, 5-person standup fixture with 3 action items | An ~11-minute fixture with **4** action items (3 with owners and due dates, 2 relative), 1 decision, 2 blockers, and 1 completion of a prior item | Covers every eval check. The extra item (Hannah chasing IT) tests an owner who isn't the speaker. |
| Postgres "backup failed" alert | A **Resource Health** activity-log alert on the server | Flexible Server exposes no backup-failure metric; Resource Health covers the incidents Azure reports for it. |
| Custom metrics for alerts | One structured `ops_metrics` log record every 5 minutes, evaluated by log alerts | No extra metrics SDK. The values (queue depth, failed jobs, subscription hours left, spend) are cheap to compute from Postgres. |
| `make` targets | Also available as `recall <command>` | The dev machine this was built on (Windows) has no `make`. `uv run recall demo` and `make demo` do the same thing. |
| Voyage (`voyage-3.5`) as an optional provider "behind the same interface" | Supported, but it needs `EMBEDDING_DIM` = 512 or 1024 set **before** the first migration | Voyage doesn't offer 384 dimensions, and the vector column's width is fixed at migration time. |

## Other choices

- **Enums as `TEXT` with CHECK constraints**, not Postgres enum types. Adding a value doesn't need `ALTER TYPE`.
- **The initial migration is frozen DDL generated from the models.** A test compares models to the migrated schema,
  so drift fails CI.
- **Speaker matching adds a last step:** a unique first name among the meeting's team and participants, for owners
  written as "Priya" in recaps.
- **Short interjections** (under 2 s, 3 words or fewer, from someone else) are folded into the current speaker's
  segment as `[Name: yep]` instead of splitting the turn.
- **The router asks Haiku only when the rules are genuinely ambiguous.** That means conflicting intents, or a
  standup word or person name without a clear verb. Anything else with no rule match goes straight to the agent.
  "Why/decision" questions always go to the agent.
- **Answer cache key:** normalized question + resolved date range, so "yesterday" asked on different days never
  shares an entry. Only answers backed by tool results are cached.
- **Effort escalation:** when no tool found anything at `low` effort, the agent reruns once at `medium`.
- **Unknown Teams users are provisioned on first message.** The person is created from `aadObjectId` and added to the
  channel's team, so end users need no setup.
- **Citations in Teams are `Action.Submit` buttons** that return the transcript excerpt as a card. This avoids
  links that would need a separate browser sign-in.
- **The local embedding model is baked into the Docker image**, so the first query is fast and works offline.
- **Evals read `ANTHROPIC_API_KEY_REAL`**, captured from `ANTHROPIC_API_KEY` before the test suite swaps in a fake.
- **`anthropic` 1.x and `mcp` 2.x are current.** `FastMCP` is now `mcp.server.mcpserver.MCPServer`.
