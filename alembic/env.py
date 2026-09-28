from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from recall.config import get_settings
from recall.db.models import Base

target_metadata = Base.metadata
MIGRATION_LOCK_ID = 7_310_001  # pg advisory lock so concurrent replicas never migrate at the same time


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_ID})
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_ID})


async def _main() -> None:
    url = context.config.attributes.get("database_url") or get_settings().database_url
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
asyncio.run(_main())
