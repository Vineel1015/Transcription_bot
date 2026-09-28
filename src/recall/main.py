"""FastAPI app. One process serves the REST API, the Teams bot and Graph webhooks, and runs the job workers and the
singleton scheduler. SIGTERM (uvicorn shutdown) stops claiming jobs and lets running ones finish for up to 30 s."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Importing these modules registers their job handlers and periodic tasks.
import recall.ingestion.graph
import recall.pipeline.run
import recall.scheduler
import recall.teams.bot  # noqa: F401
from recall.api.routes import forbidden_handler, router
from recall.config import get_settings
from recall.db.migrate import upgrade_head_async
from recall.db.session import dispose_engine
from recall.jobs import Scheduler, Worker
from recall.query.auth import Forbidden

log = logging.getLogger("recall")


def configure_telemetry(app: FastAPI) -> None:
    s = get_settings()
    if not s.applicationinsights_connection_string:
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError:
        log.warning("APPLICATIONINSIGHTS_CONNECTION_STRING is set but the 'azure' extra isn't installed")
        return
    configure_azure_monitor(
        connection_string=s.applicationinsights_connection_string,
        sampler=TraceIdRatioBased(s.otel_sample_ratio),
        logger_name="recall",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if s.run_migrations_on_startup:
        await upgrade_head_async()
    worker = Worker(concurrency=s.worker_concurrency)
    scheduler = Scheduler(s.scheduler_tick_seconds)
    if s.run_workers:
        worker.start()
        scheduler.start()
    try:
        yield
    finally:
        if s.run_workers:
            await scheduler.stop()
            await worker.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Recall", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(Forbidden, forbidden_handler)  # type: ignore[arg-type]
    configure_telemetry(app)
    return app


app = create_app()
