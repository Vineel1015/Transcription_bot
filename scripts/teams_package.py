"""`make teams-package`: build dist/recall-teams-app.zip (manifest + icons) ready to sideload or upload to the
Teams admin center. Values come from BOT_APP_ID, PUBLIC_BASE_URL and COMPANY_NAME (env or .env)."""

from __future__ import annotations

import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "recall" / "teams" / "manifest" / "manifest.json"
OUT = ROOT / "dist" / "recall-teams-app.zip"


def png(size: int, rgba: tuple[int, int, int, int], transparent_bg: bool) -> bytes:
    """A simple icon: a filled circle ("R" is left to the accent color) on a solid or transparent square."""
    r0 = size / 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inside = (x - r0 + 0.5) ** 2 + (y - r0 + 0.5) ** 2 <= (size * 0.42) ** 2
            if inside:
                row += bytes(rgba if not transparent_bg else (255, 255, 255, 255))
            else:
                row += bytes((0, 0, 0, 0) if transparent_bg else (255, 255, 255, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip().strip('"'))
    return env


def main() -> None:
    env = load_env()
    bot_id = env.get("BOT_APP_ID", "")
    base = env.get("PUBLIC_BASE_URL", "")
    if not bot_id or not base.startswith("https://"):
        sys.exit("Set BOT_APP_ID and an https PUBLIC_BASE_URL (see docs/TEAMS_SETUP.md), then re-run.")
    text = MANIFEST.read_text()
    for key, value in {
        "BOT_APP_ID": bot_id,
        "PUBLIC_BASE_URL": base.rstrip("/"),
        "PUBLIC_HOST": urlparse(base).netloc,
        "COMPANY_NAME": env.get("COMPANY_NAME", "Your company"),
    }.items():
        text = text.replace("{{" + key + "}}", value)
    json.loads(text)  # fail early on a broken manifest
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", text)
        zf.writestr("color.png", png(192, (59, 91, 219, 255), transparent_bg=False))
        zf.writestr("outline.png", png(32, (255, 255, 255, 255), transparent_bg=True))
    print(f"wrote {OUT.relative_to(ROOT)} — upload it in Teams: Apps > Manage your apps > Upload an app")


if __name__ == "__main__":
    main()
