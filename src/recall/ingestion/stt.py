"""Speech-to-text for uploaded media only (Teams transcripts need none). Deepgram with diarization when
DEEPGRAM_API_KEY is set, otherwise local faster-whisper (single "Unknown speaker")."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from recall.config import get_settings
from recall.domain import Cue

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


async def transcribe(data: bytes, filename: str) -> list[Cue]:
    s = get_settings()
    provider = s.stt_provider
    if provider == "auto":
        provider = "deepgram" if s.deepgram_api_key else "whisper_local"
    if provider == "deepgram":
        return await transcribe_deepgram(data, s.deepgram_api_key)
    return await transcribe_whisper(data, filename, s.whisper_model)


async def transcribe_deepgram(data: bytes, api_key: str) -> list[Cue]:
    params = {
        "model": "nova-3",
        "diarize": "true",
        "utterances": "true",
        "smart_format": "true",
        "punctuate": "true",
    }
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(
            DEEPGRAM_URL, params=params, content=data, headers={"Authorization": f"Token {api_key}"}
        )
        resp.raise_for_status()
    return deepgram_to_cues(resp.json())


def deepgram_to_cues(payload: dict[str, object]) -> list[Cue]:
    results = payload.get("results")
    utterances = results.get("utterances", []) if isinstance(results, dict) else []
    return [
        Cue(
            speaker=f"Speaker {int(u.get('speaker', 0)) + 1}",
            start_ms=int(float(u["start"]) * 1000),
            end_ms=int(float(u["end"]) * 1000),
            text=str(u["transcript"]).strip(),
        )
        for u in utterances
        if str(u.get("transcript", "")).strip()
    ]


async def transcribe_whisper(data: bytes, filename: str, model_size: str) -> list[Cue]:
    def run() -> list[Cue]:
        from faster_whisper import WhisperModel

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(filename).name
            path.write_bytes(data)
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            segments, _ = model.transcribe(str(path), vad_filter=True)
            return [
                Cue(
                    speaker="Unknown speaker",
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                    text=seg.text.strip(),
                )
                for seg in segments
                if seg.text.strip()
            ]

    return await asyncio.to_thread(run)
