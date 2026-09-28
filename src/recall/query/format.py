from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from recall.domain import Citation


def ms_label(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def local_date_label(started_at: dt.datetime, tz: str) -> str:
    return started_at.astimezone(ZoneInfo(tz)).strftime("%a %d %b")


def citation(
    meeting_id: uuid.UUID, title: str, started_at: dt.datetime, start_ms: int | None, tz: str = "UTC"
) -> Citation:
    ms = start_ms or 0
    label = f"{title} · {local_date_label(started_at, tz)} · {ms_label(ms)}"
    return Citation(meeting_id=meeting_id, title=title, started_at=started_at, start_ms=ms, label=label)
