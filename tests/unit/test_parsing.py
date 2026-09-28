from __future__ import annotations

import io

from recall.domain import Cue
from recall.ingestion.vtt import parse_docx, parse_text, parse_timestamp, parse_vtt
from recall.pipeline.run import detect_kind, normalize
from tests.conftest import FIXTURES


def test_timestamps() -> None:
    assert parse_timestamp("00:01:02.450") == 62_450
    assert parse_timestamp("01:02.5") == 62_500
    assert parse_timestamp("1:00:00") == 3_600_000
    assert parse_timestamp("00:00:01,200") == 1_200


def test_teams_vtt_speaker_tags() -> None:
    cues = parse_vtt((FIXTURES / "standup_2026-09-25.vtt").read_text())
    assert cues[0].speaker == "Hannah Lee"
    assert cues[0].start_ms == 4_120
    assert cues[1].speaker == "Priya Shah"
    assert "Postgres 16 migration" in cues[1].text
    assert {c.speaker for c in cues} == {
        "Hannah Lee",
        "Priya Shah",
        "Marcus Chen",
        "Aisha Okafor",
        "Diego Ramirez",
    }


def test_multiline_cue_and_missing_close_tag() -> None:
    vtt = "WEBVTT\n\nid-1\n00:00:01.000 --> 00:00:04.000\n<v Priya Shah>first line\nsecond line\n\n"
    [cue] = parse_vtt(vtt)
    assert cue.speaker == "Priya Shah"
    assert cue.text == "first line second line"


def test_vtt_without_voice_tags_uses_name_prefix() -> None:
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nMarcus Chen: shipping today\n"
    [cue] = parse_vtt(vtt)
    assert (cue.speaker, cue.text) == ("Marcus Chen", "shipping today")


def test_plain_text_transcript() -> None:
    cues = parse_text("[00:00:05] Priya: hello there\ncontinued line\n00:01:00 Marcus: hi\n")
    assert [c.speaker for c in cues] == ["Priya", "Marcus"]
    assert cues[0].text == "hello there continued line"
    assert cues[1].start_ms == 60_000


def test_docx_teams_export() -> None:
    import docx

    doc = docx.Document()
    for line in [
        "Priya Shah   0:03",
        "I finished the migration.",
        "Marcus Chen   0:15",
        "Shipping the form.",
    ]:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    cues = parse_docx(buf.getvalue())
    assert [(c.speaker, c.start_ms) for c in cues] == [("Priya Shah", 3000), ("Marcus Chen", 15000)]


def test_normalize_drops_filler_and_merges() -> None:
    cues = [
        Cue(speaker="A", start_ms=0, end_ms=3000, text="Starting the update"),
        Cue(speaker="A", start_ms=3000, end_ms=6000, text="and continuing it"),
        Cue(speaker="B", start_ms=6000, end_ms=6800, text="Um"),
        Cue(speaker="B", start_ms=6800, end_ms=7500, text="yep"),
        Cue(speaker="A", start_ms=7500, end_ms=9000, text="then finishing."),
        Cue(speaker="B", start_ms=9000, end_ms=15000, text="My turn now with a longer update"),
    ]
    segs = normalize(cues)
    assert [s.speaker_label for s in segs] == ["A", "B"]
    assert segs[0].text == "Starting the update and continuing it [B: yep] then finishing."
    assert segs[0].end_ms == 9000
    assert [s.seq for s in segs] == [0, 1]


def test_detect_kind() -> None:
    assert detect_kind("Platform Standup") == "standup"
    assert detect_kind("Daily stand-up") == "standup"
    assert detect_kind("Sprint Planning") == "planning"
    assert detect_kind("Q3 Retro") == "retro"
    assert detect_kind("Customer demo") == "other"
