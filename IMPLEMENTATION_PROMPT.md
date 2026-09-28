<role>
You are a senior engineer building a production-ready system from an empty repository in a single session. Work autonomously until every item in <success_criteria> passes. Keep the design as simple as the requirements allow: fewer services, fewer API keys, and fewer setup steps are features. When this prompt doesn't cover something, choose the simplest option, record it in docs/DECISIONS.md, and keep going instead of stopping to ask.
</role>

<product>
Build **Recall**, a meeting memory for Microsoft Teams. It works like Teams' "intelligent recap" and adds long-term memory that people can ask questions about.

1. **Capture:** When a Teams meeting with transcription enabled ends, Recall collects the speaker-attributed transcript automatically. Users can also upload a recording or transcript file.
2. **Summarize:** Within 2 minutes of the transcript being available, Recall produces a structured recap. It covers the overview, decisions, action items (owner and due date), each person's updates (done / next / blockers), and open questions. Every item carries a timestamp citation.
3. **Store:** Everything is saved in Postgres so it can be queried by team, person, date and topic.
4. **Answer:** A separate Retrieval Agent answers questions in Teams chat, for example:
   - "What did we cover in yesterday's standup?"
   - "What is Priya working on?"
   - "What's still open from the Platform sync?"
   - "Why did we drop the Redis cache?"
5. **Brief:** Ten minutes before each daily standup, Recall posts a brief of the previous working day to the team's channel.

Why speed and ease matter: people will ask these questions seconds before or during a standup. Answers that take more than a few seconds, or setup that needs more than one command, will stop people from using the tool.
</product>

<performance_targets>
- Standup-style questions ("yesterday's standup", "what did X do yesterday") answer in under 1.5 s p50. They are served from precomputed data with no LLM call (see <fast_path>).
- Open-ended questions start streaming their first token within 2 s and finish within 6 s p50.
- A standup recap is stored within 2 minutes of the transcript becoming available. Other meetings, which go through the Batch API (see <cost_controls>), are summarized within 1 hour.
- Hybrid search over 10,000 meetings returns in under 200 ms.
- Add a `make bench` target that measures these numbers against seeded data and prints a pass/fail table. The table also shows the average cost per meeting summarized and per agent question, taken from `llm_usage`.
</performance_targets>

<cost_controls>
Target running cost: under $2 per active user per month in Claude API spend, plus about $50/month of hosting. At 50 users that is roughly 5–10× cheaper than per-seat Teams Premium or Copilot licensing. Every Claude call must justify itself. The rules below are part of the spec, not optional polish.

1. **Answer without a model call when possible.** Every intent the fast path can serve costs $0. Log the questions that fall through to the agent (`agent_fallthrough` in `audit_log`) so new templated intents can be added from real usage.
2. **Answer cache.** Store each agent answer in an `answer_cache(team_id, question_hash, date_scope, answer, citations, created_at)` table. `question_hash` is computed from the question after lowercasing, trimming whitespace and resolving date expressions. Clear a team's entries whenever a new meeting for that team is indexed. Serve cache hits without any model call.
3. **Summarize each meeting once.** Answering a question must never trigger summarization. The agent reads stored recaps first and pulls raw excerpts only when a recap can't answer the question.
4. **Use the cheapest model that holds quality.** Make model choice per route and configurable:
   - Standups under 30 minutes are summarized with `claude-haiku-4-5-20251001`.
   - Other meetings use `claude-sonnet-5` at `effort: "medium"`.
   - The agent uses `claude-sonnet-5` at `effort: "low"`, and escalates to `medium` only when the first pass finds nothing.
   - Routing is rule-based first; Haiku is used only when the rules are ambiguous.
   - `make eval` must pass for the model chosen on each route. If Haiku fails the standup eval, fall back to Sonnet and record the result in DECISIONS.md.
5. **Use the Batch API for work that isn't urgent.** Standups are summarized in real time so the next brief is always ready. Other meeting kinds go through the Message Batches API at 50% off: a worker job submits pending meetings every 10 minutes and collects results by `custom_id`. The 2-minute recap target applies to standups only; other meetings get a recap within 1 hour.
6. **Make prompt caching actually work.**
   - Put the tool definitions and system prompts first, and keep them byte-for-byte stable: no timestamps, sorted keys, fixed tool order.
   - Put per-request content (the transcript, the question) after the cache breakpoint.
   - Log `cache_read_input_tokens`, and fail a test if repeated agent calls show zero cache reads.
7. **Send the model less text.**
   - Transcript format is `[mm:ss] Name: text`, with filler removed and short fragments merged.
   - Pass at most the team's 30 most recent open action items.
   - Tool results return compact JSON: IDs, titles, dates and short fields. `search` returns k=5 by default, with snippets trimmed to 300 characters.
   - `get_excerpt` returns at most 3 minutes of transcript.
8. **Cap the agent loop.** At most 4 tool calls per question. Set `max_tokens` to 1024 for answers. Send the entire tool set on every call, even when the router picks the intent, so the cached prefix stays identical between calls.
9. **Skip low-value meetings.** Only process meetings mapped to a configured team or series. Skip meetings under 3 minutes or under 300 words, and deduplicate by `external_id` before any model call.
10. **Avoid paid services where free ones work.** Teams transcripts come through Graph at no charge, so no speech-to-text is needed. Embeddings run locally by default. Paid STT is used only for uploaded media.
11. **Budgets and visibility.**
    - Record every Claude call in `llm_usage(ts, route, model, team_id, meeting_id, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, batch bool, cost_usd)`. Compute `cost_usd` from a price table in config, not hard-coded values.
    - Give each team a monthly budget (`teams.monthly_budget_usd`, default $25).
    - At 80% of the budget, notify the team's admins in Teams.
    - At 100%, open-ended questions fall back to the fast path and search-only answers, but summarization always continues, because losing recaps is worse than degraded answers.
    - `GET /v1/admin/usage` returns spend by team, route and model.
</cost_controls>

<ease_of_use>
- **End users need no setup.** They type `@Recall <question>` in a channel or 1:1 chat. They can also use `/recap` (latest meeting for this channel's team), `/standup` (the previous-working-day brief), and `/mine` (my open action items). Every answer is an Adaptive Card with short bullets and clickable citations such as "Platform Standup · Thu 25 Sep · 04:12".
- **Operators run one command.** Locally, `docker compose up` starts everything: exactly two containers, `app` and `postgres`. Migrations run on startup. `ANTHROPIC_API_KEY` is the only required secret for local use. In production, `azd up` provisions Azure and deploys (see <deployment>).
- **Teams setup is guided.** `make setup-teams` runs an interactive script that uses the `az` CLI to register the Entra app, add Graph permissions, and print the remaining admin-consent and Teams-admin commands with the exact values filled in. It also builds the Teams app package zip.
- **Configuration has sensible defaults.** Every setting has a working default. `.env.example` lists the required settings first, then the optional ones with comments.
</ease_of_use>

<stack>
Use this stack. It was chosen to keep the number of moving parts low.

- Python 3.12, `uv`, FastAPI, SQLAlchemy 2.0 async, Alembic. Code must pass `ruff` and `mypy --strict`.
- PostgreSQL 16 with `pgvector` is the only datastore. It is also the job queue: use a `jobs` table claimed with `SELECT … FOR UPDATE SKIP LOCKED` by async workers running in the same `app` process. Do not add Redis or Celery.
- Claude, through the Anthropic Python SDK. Models are chosen per route as described in <cost_controls>:
  - Summarizer: `claude-haiku-4-5-20251001` for short standups, `claude-sonnet-5` for everything else.
  - Retrieval Agent: `claude-sonnet-5`, streaming.
  - Intent router, only when rules are ambiguous: `claude-haiku-4-5-20251001`.
  - All model IDs and effort levels are configurable through env vars.
  - Use prompt caching, and the Message Batches API for meetings that aren't urgent.
- Embeddings: local `fastembed` (`BAAI/bge-small-en-v1.5`, 384 dimensions) by default, so no extra key is needed. Voyage (`voyage-3.5`) is an optional provider behind the same interface.
- Speech-to-text (upload path only, because Teams provides its own transcripts): Deepgram with diarization if `DEEPGRAM_API_KEY` is set, otherwise local `faster-whisper`.
- Teams:
  - Microsoft Graph through `httpx`, with app-only auth using `msal`.
  - Bot Framework through `botbuilder-core`, on the same FastAPI app at `/api/messages`.
- MCP: a `recall-mcp` stdio entrypoint that exposes the retrieval tools read-only, so Claude Desktop and Claude Code can query meeting memory.
</stack>

<architecture>
```
Teams Graph webhook ─┐                                  ┌─► Teams bot (@Recall, /recap, /standup, /mine)
Graph poll (15 min) ─┼─► jobs table ─► pipeline ─► Postgres ◄─┼─► REST  POST /v1/ask (SSE stream)
Upload endpoint ─────┘   (SKIP LOCKED) │                      ├─► MCP server
                                       │                      └─► Standup brief scheduler
       normalize → match speakers → summarize → persist → embed → precompute briefs
```

Keep one boundary strict: the summarizer writes to the database, and every reader (Retrieval Agent, bot fast path, REST, MCP, scheduler) goes only through `recall/query/`. The Retrieval Agent never builds SQL. This keeps authorization and performance tuning in one place.
</architecture>

<layout>
```
src/recall/
  config.py  main.py (FastAPI app + worker startup)  jobs.py
  db/models.py  db/session.py   alembic/
  ingestion/  graph.py  upload.py  vtt.py  stt.py
  pipeline/   run.py  speakers.py  summarize.py  embed.py  briefs.py
  query/      meetings.py  people.py  actions.py  search.py  dates.py  auth.py
  agent/      router.py  agent.py  tools.py  prompts.py
  teams/      bot.py  cards.py  manifest/
  mcp_server.py
scripts/setup_teams.py  scripts/seed_demo.py  scripts/bench.py
infra/  main.bicep  main.parameters.json  modules/ (containerapp, postgres, keyvault, storage, bot, monitoring, registry)
azure.yaml   .github/workflows/ci.yml  .github/workflows/deploy.yml
tests/fixtures/ (Teams VTT files, Graph JSON payloads)  tests/unit/  tests/integration/  tests/evals/
docs/ README.md  TEAMS_SETUP.md  DEPLOY.md  PRIVACY.md  DECISIONS.md
```
</layout>

<data_model>
Tables. Use UUID primary keys unless noted, add `created_at` everywhere, and give foreign keys `ON DELETE CASCADE`.

- `teams(name, channel_id, timezone, standup_time, holidays date[], monthly_budget_usd numeric default 25)`
- `people(display_name, email unique, entra_id unique, aliases text[])`, plus `team_members(team_id, person_id)`
- `meetings(team_id, title, kind [standup|planning|retro|sync|other], source [teams|upload], external_id, started_at, ended_at, status [queued|summarized|indexed|failed], error)`, `unique(source, external_id)`, index `(team_id, started_at desc)`
- `participants(meeting_id, person_id)`
- `segments(bigserial id, meeting_id, seq, speaker_label, person_id null, start_ms, end_ms, text)`
- `recaps(meeting_id unique, model, prompt_version, overview, topics jsonb, decisions jsonb, open_questions jsonb)`
- `action_items(meeting_id, description, owner_id null, owner_text, due_date, status [open|done|dropped], cite_ms)`, index `(owner_id, status)`
- `person_updates(meeting_id, person_id null, speaker_label, done text[], next text[], blockers text[], cite_ms)`, index `(person_id, meeting_id)`
- `chunks(bigserial id, meeting_id, text, start_ms, end_ms, embedding vector(384), tsv tsvector generated)`, with an HNSW cosine index on `embedding` and a GIN index on `tsv`
- `briefs(team_id, for_date, content jsonb, rendered_card jsonb, unique(team_id, for_date))`. These are precomputed answers.
- `jobs(bigserial id, kind, payload jsonb, status, attempts, run_after, last_error)`
- `audit_log(actor_id, action, target, at)`
- `answer_cache(team_id, question_hash, date_scope, answer, citations jsonb, created_at)`, `unique(team_id, question_hash, date_scope)`
- `llm_usage(bigserial id, ts, route, model, team_id, meeting_id null, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, batch bool, cost_usd numeric)`, index `(team_id, ts)`
</data_model>

<ingestion>
**Teams (primary).**
- Required Graph application permissions: `OnlineMeetings.Read.All`, `OnlineMeetingTranscript.Read.All`, `User.Read.All`.
- Subscribe to transcript-created change notifications (`communications/onlineMeetings/getAllTranscripts`). Handle the validation token, verify `clientState`, and auto-renew the subscription before it expires.
- On each notification, fetch the meeting and then the transcript as `text/vtt`, and enqueue it.
- A poll job every 15 minutes catches any missed webhooks. Deduplicate on `external_id`.
- Check these Graph paths against current Microsoft docs, and record any differences in DECISIONS.md.
- Parse `<v Speaker Name>` tags and merge consecutive cues from the same speaker.
- Map each meeting to a team by its join URL or a per-team title pattern.

**Upload.**
- `POST /v1/upload` accepts audio/video or `.vtt`/`.txt`/`.docx`, with fields `team_id`, `title`, `started_at`.
- Diarized speakers come in as "Speaker N". `PATCH /v1/meetings/{id}/speakers` maps them to people and re-runs extraction only, not STT.
</ingestion>

<pipeline>
Each stage is idempotent and updates `meetings.status`. Retry with backoff up to 3 attempts.

1. **normalize:** Drop filler-only cues and merge fragments under 2 s. Then apply the skip rules in <cost_controls>: a skipped meeting is marked `status='indexed'` with `error='skipped: <reason>'`, and no model is called.
2. **match speakers:** Match speakers to people using, in order, Entra ID from Graph participants, email, alias, and fuzzy name match (ratio ≥ 0.9). Leave unmatched speakers as null.
3. **summarize:** Make one Claude call using forced tool-use with the `submit_recap` tool, whose schema is derived from a pydantic model:
   ```json
   {"overview": "", "kind": "standup|planning|retro|sync|other",
    "topics": [{"title": "", "summary": "", "start_ms": 0}],
    "decisions": [{"decision": "", "rationale": "", "start_ms": 0}],
    "action_items": [{"description": "", "owner": "name|null", "due": "YYYY-MM-DD|null", "start_ms": 0}],
    "person_updates": [{"speaker": "", "done": [], "next": [], "blockers": [], "start_ms": 0}],
    "completed_prior_action_item_ids": [],
    "open_questions": []}
   ```
   - Input: transcript lines formatted as `[mm:ss] Name: text`, the meeting date, participants, and the team's currently open action items. Passing the open items lets Claude mark items done when someone reports finishing them.
   - Put the transcript first and the instructions after it, since long-context accuracy is better that way.
   - Summarizer system prompt, with reasons included so Claude generalizes well:
     - Use only facts stated in the transcript, because people act on these recaps.
     - Never guess an owner or date. Leave it null instead.
     - Resolve relative dates such as "by Friday" against the meeting date.
     - Keep each update in the speaker's own words, trimmed.
     - Every item needs `start_ms`, so users can verify it.
     - `person_updates` is required for standups.
   - Only if the transcript exceeds 150k tokens, summarize windows in parallel and then merge them.
   - If validation fails, retry once with the validation error included.
4. **persist:** Write the recap, action items and person updates, and close any prior items the recap marked as completed.
5. **embed:** Embed ~300-token sliding windows over the segments, with speaker names inlined and 15% overlap, plus one chunk per recap section. Process in batches.
6. **precompute:** Build or refresh `briefs` for the team for the next working day. This is what makes the fast path fast.
</pipeline>

<fast_path>
Most questions are about recent standups, so answer them without the agent loop.

`agent/router.py` classifies each question:
- First with regex and keyword rules (for example, the slash commands and phrases like "yesterday" plus "standup").
- Then, only if the rules are ambiguous, with one `claude-haiku-4-5-20251001` call.

It returns one of these intents: `standup_brief`, `person_recent`, `my_actions`, `meeting_recap`, `open_question`.

The first four intents are answered straight from `briefs`, `person_updates`, `action_items` and `recaps` with templated Adaptive Cards and no Sonnet call. Only `open_question` goes to the Retrieval Agent.

Date words resolve in `query/dates.py` using the team's timezone, skipping weekends and team holidays. For example, "yesterday" on a Monday means Friday. This logic lives in code, not in the LLM.
</fast_path>

<retrieval_agent>
- Uses `claude-sonnet-5` with streaming, up to 4 tool calls, and parallel tool calls enabled. The system prompt and tool definitions are prompt-cached. Check `answer_cache` before calling the model (see <cost_controls>).
- Tools, each a thin wrapper over `recall/query/` that enforces the asker's permissions:
  - `resolve_dates(expression, team_id)`
  - `list_meetings(team_id?, kind?, start, end, participant?)`
  - `get_recap(meeting_id)`
  - `get_person_updates(person, start, end)`
  - `get_action_items(status?, owner?, team_id?, since?)`
  - `search(query, team_id?, start?, end?, k=5)`: hybrid search, combining pgvector cosine and `ts_rank` results with reciprocal rank fusion
  - `get_excerpt(meeting_id, start_ms, end_ms)`: at most 3 minutes
- System prompt:
  - Answer only from tool results.
  - Cite the meeting title, date and timestamp for every claim.
  - If nothing relevant is found, say so in one sentence rather than guessing.
  - Lead with the direct answer in 1–5 bullets, because people read this in a chat pane during a meeting.
  - Call independent tools in parallel.
- Authorization: a user can see a meeting if they attended it or are a member of its team. Admins can see everything. Log every question to `audit_log`.
</retrieval_agent>

<interfaces>
- **Teams bot:**
  - Answers @mentions, 1:1 chats, and the `/recap`, `/standup` and `/mine` commands.
  - The caller's identity comes from `aadObjectId`.
  - Stream answers: send a "Thinking…" card immediately, then update it in place.
  - Include `teams/manifest/` and have `make teams-package` build a zip ready to sideload.
- **REST:**
  - `POST /v1/ask` streams its answer as SSE and returns `{answer, citations[]}`.
  - `GET /v1/meetings`, `GET /v1/meetings/{id}`, `GET /v1/action-items`, `PATCH /v1/action-items/{id}`, `POST /v1/upload`, `POST /graph/notifications`, `GET /healthz`.
  - Auth uses an Entra JWT; `DEV_AUTH=true` allows a static local token.
- **MCP:** `recall-mcp` exposes the retrieval tools read-only.
- **Standup scheduler:** Runs 10 minutes before each team's `standup_time` on working days. It posts the precomputed brief (per-person done/next/blockers, overdue or due-today action items, unresolved blockers) to the team's channel. If nothing was recorded, it posts nothing.
</interfaces>

<deployment>
Production runs on Azure, next to the Microsoft 365 tenant, so Graph and bot traffic stays low-latency and data stays in one cloud. The aim is one command, `azd up` (Azure Developer CLI), from an empty Azure subscription to a running system.

**Resources.** All are defined in Bicep under `infra/`, in one resource group per environment (`dev`, `prod`):
| Resource | Configuration | Why |
|---|---|---|
| Azure Container Apps (Consumption) + Container Apps environment | The `app` container: 1 vCPU / 2 GiB, `minReplicas: 1`, `maxReplicas: 3`, HTTP scaling at 50 concurrent requests. Liveness probe on `/healthz`; readiness probe on `/healthz?deep=1`, which checks the database. | Always on, because webhooks, the standup scheduler and fast answers can't wait for a cold start. Keep it to one image and one app. |
| Azure Container Apps Job (manual trigger) | Runs `alembic upgrade head` with the same image | Migrations run once per deploy, before the new revision takes traffic |
| Azure Database for PostgreSQL – Flexible Server | PostgreSQL 16, Burstable B1ms to start (B2s once there are more than ~100 users), 32 GiB storage with auto-grow, 7-day point-in-time restore, `azure.extensions = VECTOR`. Private access through a VNet integrated with the Container Apps environment. | Managed backups and pgvector, with no public endpoint |
| Azure Container Registry (Basic) | Stores the image; the app pulls it using managed identity | No registry passwords |
| Azure Key Vault | Holds `ANTHROPIC_API_KEY`, Graph and bot secrets, `GRAPH_CLIENT_STATE`, and any optional provider keys. Container Apps reads them as Key Vault secret references through a user-assigned managed identity. | No secrets in env files or the pipeline |
| Azure Blob Storage (Standard LRS) | Uploaded media in a private container. A lifecycle rule deletes blobs after the media retention period (30 days). | Cheap storage, and retention enforced by the platform as well as the app |
| Azure Bot resource | Single-tenant, messaging endpoint `https://<app-fqdn>/api/messages`, Microsoft Teams channel enabled | Required for the Teams bot. The Teams channel is free. |
| Log Analytics + Application Insights | The app exports traces, metrics and logs with OpenTelemetry (`azure-monitor-opentelemetry`). Custom metrics: answer latency by intent, `llm_usage` cost, job queue depth, failed jobs. | One place to see performance and cost |
| Alerts (Azure Monitor) | Fire when: 5xx rate > 2% for 5 minutes, job queue depth > 50 for 15 minutes, a job fails 3 times, the Graph subscription is within 12 hours of expiry, a Postgres backup fails, or daily Claude spend is more than 150% of the 7-day average | Operators hear about problems before users do |

**App changes needed for production:**
- Add `MEDIA_STORAGE=azure_blob` (a new default in production) using `azure-storage-blob` with managed identity. Keep `local` and `s3` as options.
- Use `DefaultAzureCredential` for Blob and Key Vault access. There are no connection-string secrets for Azure resources, except the Postgres password, which is stored in Key Vault. Use Entra authentication for Postgres if `azd` supports it cleanly, and record the choice in DECISIONS.md.
- **Safe with multiple replicas.** The standup scheduler, Graph subscription renewal, poll job, Batch submitter and retention purge each run under a Postgres advisory lock (`pg_try_advisory_lock`), so exactly one replica runs each of them. Job workers are already safe because of `SKIP LOCKED`.
- **Graceful shutdown.** On SIGTERM, stop claiming jobs, finish the jobs in progress within 30 seconds, and return any unfinished ones to the queue.
- The webhook URL (`GRAPH_WEBHOOK_URL`) and bot endpoint come from the Container App FQDN or a custom domain. `azd` outputs both. Support an optional custom domain with a Container Apps managed certificate.

**CI/CD** (`.github/workflows/`):
- `ci.yml` runs on every pull request: `ruff`, `mypy --strict`, `make test` against a Postgres service container, `az bicep build`, and a Docker build.
- `deploy.yml` runs on every push to `main`. It logs in to Azure with OIDC federated credentials (no stored Azure secrets), builds and pushes the image tagged with the git SHA, runs the migration job, then deploys a new Container Apps revision. It then runs smoke checks: `/healthz?deep=1` and a canned fast-path question with `DEV_AUTH` off, using a service token. On failure it shifts traffic back to the previous revision.
- Production deploys need approval through a GitHub `prod` environment protection rule.

**Estimated monthly cost** at ~50 users (document it in the README and label it as an estimate to check with the Azure pricing calculator):
| Item | Cost |
|---|---|
| Container Apps, one always-on replica | ~$25–45 |
| Postgres B1ms plus storage | ~$15–25 |
| Container Registry, Key Vault, Blob, Log Analytics (with a daily ingestion cap) | ~$10–15 |
| **Total hosting** | **~$50–85**, plus Claude API usage |

Set the Log Analytics daily cap to 1 GB, and sample traces at 20% so monitoring costs don't grow unexpectedly.

**Data residency.** One `AZURE_LOCATION` parameter sets the region for every resource, and it should match the M365 tenant's region. Document in PRIVACY.md that transcripts are sent to the Anthropic API for processing, and how to route Claude calls through Microsoft Foundry instead (use the `AnthropicFoundry` client, selected by config) if the organization requires Azure-contained processing.

**Operator docs.** Add `docs/DEPLOY.md` with these sections:
- Prerequisites (`az`, `azd`, Docker, owner rights on the subscription)
- `azd auth login && azd up`
- Setting secrets in Key Vault
- Pointing the Graph subscription and bot at the new FQDN
- Rolling back a revision
- Restoring Postgres to a point in time
- Rotating secrets
- Scaling up the database tier
</deployment>

<privacy>
- Only process meetings where Teams transcription was enabled, which means participants saw Teams' notice, or files that were explicitly uploaded.
- Retention defaults, configurable per team:
  - Media: 30 days
  - Transcripts and chunks: 180 days
  - Recaps and action items: 2 years
- A nightly purge job enforces retention.
- Never log transcript text at INFO level. Read secrets from env only.
- Document all of this in docs/PRIVACY.md.
</privacy>

<testing>
- **Fixtures:** Write a realistic 15-minute, 5-person standup VTT with Teams speaker tags. Include 3 action items (one with a relative due date), 1 decision, 2 blockers, and one person reporting that they finished a previously open item. Add a 30-minute planning meeting fixture, and recorded Graph webhook and API payloads.
- **Unit tests:** VTT parsing, speaker matching, date resolution (Monday → Friday, holidays, timezones), RRF, router intents, authorization, schema validation.
- **Integration tests:** Use real Postgres and mock Anthropic and Graph with recorded responses. Cover webhook → pipeline → rows, upload → recap, each fast-path intent, the `/v1/ask` stream, and bot activity → card.
- **Evals (`make eval`):** Uses the real API and is skipped when no key is set. On the standup fixture, check that every action item is found with the correct owner, that no owner is invented, that blockers are attributed correctly, and that the prior item is closed. Then ask the agent 6 scripted questions and check the expected facts and citations.
- **Benchmarks:** `make bench` checks the numbers in <performance_targets>.
</testing>

<working_method>
- Build in this order, running the tests after each step before moving on:
  1. Scaffold, compose, models, migrations
  2. VTT parser and upload path
  3. Pipeline and summarizer
  4. Query layer, router and fast path
  5. Retrieval Agent and `/v1/ask`
  6. MCP server
  7. Graph ingestion
  8. Teams bot, cards and scheduler
  9. STT, retention and the `setup-teams` script
  10. Production hardening: advisory locks, graceful shutdown, Blob storage, OpenTelemetry
  11. Bicep infrastructure, `azure.yaml`, CI/CD workflows
  12. Docs, evals and bench
- Keep a running checklist in `PROGRESS.md` so the work can resume if context is reset.
- When reads or commands don't depend on each other, run them in parallel.
- Write general, correct code. Don't special-case the test fixtures or weaken tests to make them pass. If a test seems wrong, fix the test and explain why in DECISIONS.md.
- Don't add services, abstractions or features beyond this spec.
</working_method>

<success_criteria>
- On a clean machine with only `ANTHROPIC_API_KEY` set, `docker compose up` followed by `make demo` must:
  - seed a team and people,
  - ingest the standup fixture,
  - print its recap,
  - answer "What did everyone do in yesterday's standup?" via the fast path, and
  - answer an open-ended question via the agent, with citations.
- `make test` passes, and `ruff` and `mypy --strict src` are clean.
- `make bench` meets every target in <performance_targets>.
- Every item in <cost_controls> is implemented. Integration tests cover:
  - an answer-cache hit makes no model call,
  - the cache is cleared when a new meeting is indexed,
  - a meeting under 3 minutes is skipped before any model call,
  - non-standup meetings are routed to the Batch API,
  - a team over budget falls back to non-LLM answers while summarization continues,
  - prompt cache reads are non-zero on repeated agent calls.
- `make setup-teams` and `docs/TEAMS_SETUP.md` take an admin from nothing to a working bot. This includes the Entra app, permissions, admin consent, the Teams application access policy, enabling transcription, a dev tunnel for the webhook, and sideloading.
- Deployment is ready to run:
  - `az bicep build --file infra/main.bicep` succeeds with no errors.
  - `azd` config validates.
  - Both workflows pass `actionlint`.
  - An integration test runs two app instances against one database and shows each singleton job (scheduler, subscription renewal, poll, batch submitter, purge) runs exactly once per tick.
  - A test sends SIGTERM mid-job and shows the job is returned to the queue.
  - Actually provisioning Azure is a manual step for the human. List it in the final report.
- `README.md` fits on one screen for users (how to ask Recall things in Teams). The operator setup goes below it.
</success_criteria>

<final_report>
When you finish, reply with:
- what was built
- the benchmark results table
- any deviations from this spec and the reasons
- a short checklist of the manual steps a human still has to do (Azure/Teams admin actions and API keys)
</final_report>
