# Progress

Build order from `IMPLEMENTATION_PROMPT.md`. Each step's tests passed before moving on.

- [x] 1. Scaffold, compose, models, migrations
- [x] 2. VTT parser and upload path
- [x] 3. Pipeline and summarizer (Haiku for short standups, Batch API for the rest)
- [x] 4. Query layer, router and fast path
- [x] 5. Retrieval Agent and `/v1/ask` (JSON + SSE), answer cache, budget fallback
- [x] 6. MCP server (`recall-mcp`)
- [x] 7. Graph ingestion (webhook, subscription renewal, poll fallback)
- [x] 8. Teams bot, cards, pre-standup scheduler
- [x] 9. STT, retention purge, `setup-teams` script
- [x] 10. Production hardening: advisory locks, graceful shutdown, Blob storage, OpenTelemetry
- [x] 11. Bicep infrastructure, `azure.yaml`, CI/CD workflows
- [x] 12. Docs, evals, bench

## Verified on the build machine

- `ruff check`, `ruff format --check` and `mypy --strict src scripts`: clean
- `pytest`: all unit and integration tests pass (embedded Postgres 16 + pgvector)
- `recall bench --meetings 10000`: standup fast path p50 23 ms, hybrid search p50 18 ms (hash embedder)
- `actionlint`: clean
- `recall teams-package`: produces a valid zip

## Not verified here

- The Docker image build and `docker compose up`: Docker Desktop wasn't running on the build machine
- `az bicep build`: no Azure CLI on the build machine; CI runs it on every PR
- Real Claude calls (`make demo`, `make eval`, agent latency rows in `make bench`): no API key on the build machine
- The local fastembed model (tests and bench used the hash embedder, to avoid a model download)
- Anything against a real tenant (Graph, bot, Azure deploy)
