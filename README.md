# Recall — meeting memory for Microsoft Teams

Recall reads the transcripts of your team's Teams meetings, writes a recap (decisions, action items, what each
person did and is doing next, blockers), and answers questions about them in Teams, with a citation for every claim.

## Using it (for everyone)

Mention **@Recall** in your team's channel, or chat with it directly:

| Ask | You get |
|---|---|
| `/standup` or "what did we cover in yesterday's standup?" | Everyone's done / next / blockers from the last working day |
| "What is Priya working on?" | Her latest updates |
| `/mine` | Your open action items |
| `/recap` | The latest meeting's recap |
| "Why did we drop the Redis cache?" | An answer written from recaps and transcripts |

- Every answer links to its sources, such as **Platform Standup · Fri 25 Sep · 04:12**. Click one to read that part of the transcript.
- Ten minutes before your standup, Recall posts a brief of the previous working day in your team's channel.
- Recall only processes meetings where Teams transcription was turned on. You'll see Teams' own notice when it is.

---

## Running it (for operators)

**Local development.** You need Docker and an Anthropic API key.

```bash
cp .env.example .env              # set ANTHROPIC_API_KEY
docker compose up -d --build      # two containers: app + postgres; migrations run on start
docker compose exec app recall demo
```

The demo seeds a team, ingests a sample standup and planning meeting, prints the recap, and answers a fast-path
question and an open question, each with citations. `make up` / `make demo` do the same where `make` is available.

**Without Docker:** `uv sync`, point `DATABASE_URL` at a Postgres 16 server with pgvector, then `uv run recall serve`.

| Command | What it does |
|---|---|
| `uv run pytest` / `make test` | Unit and integration tests. Uses `TEST_DATABASE_URL`, or starts an embedded Postgres |
| `make lint`, `make typecheck` | `ruff` and `mypy --strict` |
| `uv run recall bench` | Latency targets and cost per meeting/question, against 10,000 seeded meetings |
| `uv run recall eval` | Real-API evals of the summarizer and agent (needs the key) |
| `uv run recall setup-teams` | Guided Entra app registration and Graph permissions (needs the `az` CLI) |
| `uv run recall teams-package` | Builds `dist/recall-teams-app.zip` for Teams |
| `recall-mcp` | Read-only MCP server so Claude Desktop and Claude Code can query meeting memory |

**Asking questions over HTTP.** `POST /v1/ask {"question": "...", "team_id": "..."}` returns
`{answer, citations}`. Send `Accept: text/event-stream` to stream it instead. The full OpenAPI docs are at `/docs`.
Locally, with `DEV_AUTH=true`, use `Authorization: Bearer dev` plus `X-Dev-User: you@company.com`.

**MCP.** Add this to your Claude Desktop or Claude Code MCP config:

```json
{"mcpServers": {"recall": {"command": "uv", "args": ["run", "--directory", "/path/to/recall", "recall-mcp"],
  "env": {"DATABASE_URL": "...", "MCP_USER_EMAIL": "you@company.com"}}}}
```

**Docs**

| Document | Covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works, cost controls, the data model |
| [docs/TEAMS_SETUP.md](docs/TEAMS_SETUP.md) | Entra app, permissions, access policy, transcription, bot |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Azure with `azd up`, CI/CD, rollback, restore, scaling |
| [docs/PRIVACY.md](docs/PRIVACY.md) | Consent, retention, where data goes |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Choices made while building, and deviations from the spec |
