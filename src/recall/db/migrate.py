from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[3]


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    if database_url:
        cfg.attributes["database_url"] = database_url
    return cfg


def upgrade_head(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")


async def upgrade_head_async(database_url: str | None = None) -> None:
    # alembic's env.py runs its own event loop, so run it in a worker thread.
    await asyncio.to_thread(upgrade_head, database_url)
