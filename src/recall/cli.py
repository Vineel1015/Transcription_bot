"""`recall` command: serve, migrate, demo, bench, eval, teams-package, setup-teams.

On machines without make, `uv run recall <command>` (or `docker compose exec app recall <command>`) does the same
as the Makefile targets."""

from __future__ import annotations

import argparse
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str, argv: list[str]) -> None:
    sys.argv = [name, *argv]
    runpy.run_path(str(ROOT / "scripts" / f"{name}.py"), run_name="__main__")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="recall")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the API, bot, workers and scheduler")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    sub.add_parser("migrate", help="apply database migrations")
    for name, help_text in [
        ("demo", "seed a team, ingest the standup fixture, print its recap and answer two questions"),
        ("bench", "measure latency targets and cost per meeting/question"),
        ("teams-package", "build the Teams app zip"),
        ("setup-teams", "interactive Entra app + Graph permissions setup (needs the az CLI)"),
    ]:
        sub.add_parser(name, help=help_text, add_help=False)
    sub.add_parser("eval", help="run real-API evals (needs ANTHROPIC_API_KEY)")
    args, rest = parser.parse_known_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "recall.main:app",
            host=args.host,
            port=args.port,
            proxy_headers=True,
            timeout_graceful_shutdown=35,
        )
    elif args.command == "migrate":
        from recall.db.migrate import upgrade_head

        upgrade_head()
        print("migrations applied")
    elif args.command == "eval":
        sys.exit(
            subprocess.call(
                [sys.executable, "-m", "pytest", "-m", "eval", "tests/evals", "-v", *rest], cwd=ROOT
            )
        )
    else:
        script = {
            "demo": "seed_demo",
            "bench": "bench",
            "teams-package": "teams_package",
            "setup-teams": "setup_teams",
        }[args.command]
        _script(script, rest)


if __name__ == "__main__":
    main()
