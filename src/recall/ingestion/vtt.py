"""Transcript parsers: Teams WebVTT (with <v Speaker> tags), plain text and Teams .docx exports."""

from __future__ import annotations

import io
import itertools
import re

from recall.domain import Cue

_TIMING = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
_VOICE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]+)>(.*?)(?:</v>|$)", re.DOTALL)
_TAG = re.compile(r"</?[^>]+>")
# "[00:01:02] Name: text", "00:01:02 Name: text", "Name: text"
_TXT_LINE = re.compile(
    r"^\s*(?:\[?(?P<ts>(?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\]?\s+)?(?P<name>[^:\[\]]{1,60}?):\s+(?P<text>.+)$"
)
# Teams .docx export: a header line "Priya Shah   0:03" followed by the utterance on the next paragraph.
_DOCX_HEADER = re.compile(r"^(?P<name>.+?)\s{2,}(?P<ts>(?:\d+:)?\d{1,2}:\d{2})\s*$")


def parse_timestamp(value: str) -> int:
    """'01:02:03.450', '02:03.450', '2:03' or '1:02:03' -> milliseconds."""
    value = value.replace(",", ".")
    parts = value.split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return round(((hours * 60 + minutes) * 60 + seconds) * 1000)


def parse_vtt(content: str) -> list[Cue]:
    content = content.replace("\r\n", "\n").lstrip("﻿")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line for line in block.split("\n") if line.strip()]
        timing_idx = next((i for i, line in enumerate(lines) if _TIMING.match(line)), None)
        if timing_idx is None:
            continue  # WEBVTT header, NOTE, STYLE blocks
        match = _TIMING.match(lines[timing_idx])
        assert match is not None
        start, end = parse_timestamp(match["start"]), parse_timestamp(match["end"])
        payload = "\n".join(lines[timing_idx + 1 :])
        voices = _VOICE.findall(payload)
        if voices:
            for speaker, text in voices:
                cleaned = _clean(text)
                if cleaned:
                    cues.append(Cue(speaker=speaker.strip(), start_ms=start, end_ms=end, text=cleaned))
            continue
        cleaned = _clean(payload)
        if not cleaned:
            continue
        # Some exporters write "Name: text" instead of voice tags.
        named = re.match(r"^([^:]{1,60}):\s+(.+)$", cleaned, re.DOTALL)
        if named:
            cues.append(Cue(speaker=named[1].strip(), start_ms=start, end_ms=end, text=named[2].strip()))
        else:
            cues.append(Cue(speaker="Unknown speaker", start_ms=start, end_ms=end, text=cleaned))
    return cues


def parse_text(content: str) -> list[Cue]:
    """Plain-text transcripts: one 'Name: text' line per utterance, optional leading timestamp.
    Lines without a speaker continue the previous utterance. Missing timestamps are estimated at
    150 words per minute so citations stay roughly ordered."""
    cues: list[Cue] = []
    clock_ms = 0
    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        match = _TXT_LINE.match(line)
        if match:
            start = parse_timestamp(match["ts"]) if match["ts"] else clock_ms
            text = match["text"].strip()
            cues.append(Cue(speaker=match["name"].strip(), start_ms=start, end_ms=start, text=text))
        elif cues:
            cues[-1].text = f"{cues[-1].text} {line}"
        else:
            cues.append(Cue(speaker="Unknown speaker", start_ms=clock_ms, end_ms=clock_ms, text=line))
        last = cues[-1]
        duration = int(len(last.text.split()) / 150 * 60_000)
        last.end_ms = max(last.end_ms, last.start_ms + duration)
        clock_ms = last.end_ms
    return cues


def parse_docx(data: bytes) -> list[Cue]:
    import docx

    document = docx.Document(io.BytesIO(data))
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    cues: list[Cue] = []
    pending: tuple[str, int] | None = None
    for text in paragraphs:
        header = _DOCX_HEADER.match(text)
        if header:
            pending = (header["name"].strip(), parse_timestamp(header["ts"]))
            continue
        if pending is not None:
            name, start = pending
            cues.append(Cue(speaker=name, start_ms=start, end_ms=start, text=text))
            pending = None
        elif cues:
            cues[-1].text = f"{cues[-1].text} {text}"
    if not cues:
        return parse_text("\n".join(paragraphs))
    for current, following in itertools.pairwise(cues):
        current.end_ms = max(current.start_ms, following.start_ms)
    cues[-1].end_ms = cues[-1].start_ms + int(len(cues[-1].text.split()) / 150 * 60_000)
    return cues


def parse_transcript_file(filename: str, data: bytes) -> list[Cue]:
    name = filename.lower()
    if name.endswith(".docx"):
        return parse_docx(data)
    text = data.decode("utf-8-sig", errors="replace")
    if name.endswith(".vtt") or text.lstrip().startswith("WEBVTT"):
        return parse_vtt(text)
    return parse_text(text)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", text)).strip()
