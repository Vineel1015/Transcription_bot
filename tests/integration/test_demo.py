"""`make demo` end to end, with a fake Claude (the real run needs ANTHROPIC_API_KEY)."""

from __future__ import annotations

import runpy
from typing import Any

import pytest

from tests.conftest import ROOT_DIR, load_recap
from tests.fakes import FakeAnthropic, FakeMessage, json_message, text_message


async def test_demo_script(
    fake_claude: FakeAnthropic, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from recall.config import get_settings

    # the demo turns off the Batch API in-process; restore it for the tests that follow
    monkeypatch.setattr(get_settings(), "use_batch_api", get_settings().use_batch_api)

    def handle(params: dict[str, Any]) -> FakeMessage:
        if "format" in params.get("output_config", {}):
            return json_message(load_recap())
        return text_message("- Postgres alone had a 4 ms p99, so the cache wasn't needed.")

    fake_claude.handler = handle
    demo = runpy.run_path(str(ROOT_DIR / "scripts" / "seed_demo.py"))
    await demo["main"]()
    out = capsys.readouterr().out
    assert "Standup recap" in out and "decision: Drop the Redis cache" in out
    assert "(standup_brief, " in out  # fast path
    assert "(open_question, " in out  # agent
    assert "Claude call(s) recorded" in out

    await demo["main"]()  # idempotent: re-running doesn't duplicate meetings
    assert "processed" not in capsys.readouterr().out
