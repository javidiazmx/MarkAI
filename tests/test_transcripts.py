"""Transcript parsing for the four formats the owner might hand us."""

from __future__ import annotations

import json

import pytest

from markai.ingest.transcripts import (
    format_timestamp,
    merge_segments,
    parse_transcript_file,
    segments_to_text,
    slugify,
)
from markai.models import IngestError, Segment

SRT = """1
00:00:01,000 --> 00:00:04,000
Security deposits in Chicago

2
00:00:04,500 --> 00:00:08,000
must sit in a separate account.
"""

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Heat season starts September fifteenth

00:00:04.000 --> 00:00:07.500
and runs through June first.
"""


def test_srt_parsing(tmp_path):
    path = tmp_path / "a.srt"
    path.write_text(SRT, encoding="utf-8")
    segments = parse_transcript_file(path)
    assert len(segments) == 2
    assert segments[0].start == 1.0 and segments[0].end == 4.0
    assert "Security deposits" in segments[0].text


def test_vtt_parsing(tmp_path):
    path = tmp_path / "a.vtt"
    path.write_text(VTT, encoding="utf-8")
    segments = parse_transcript_file(path)
    assert len(segments) == 2
    assert segments[1].text.endswith("June first.")


def test_json_list_shape_from_youtube(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps(
            [
                {"text": "one", "start": 0.0, "duration": 2.0},
                {"text": "two", "start": 2.0, "duration": 3.0},
            ]
        ),
        encoding="utf-8",
    )
    segments = parse_transcript_file(path)
    assert [s.end for s in segments] == [2.0, 5.0]


def test_json_segments_shape(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(
        json.dumps({"segments": [{"text": "one", "start": 0.0, "end": 4.0}]}), encoding="utf-8"
    )
    segments = parse_transcript_file(path)
    assert segments[0].end == 4.0


def test_plain_text_has_no_timing(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
    segments = parse_transcript_file(path)
    assert len(segments) == 2
    assert all(s.start == 0.0 and s.end == 0.0 for s in segments)


def test_empty_file_yields_nothing(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    assert parse_transcript_file(path) == []


def test_unknown_suffix_is_an_ingest_error(tmp_path):
    path = tmp_path / "a.docx"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(IngestError):
        parse_transcript_file(path)


def test_missing_file_is_an_ingest_error(tmp_path):
    with pytest.raises(IngestError):
        parse_transcript_file(tmp_path / "nope.srt")


def test_segments_to_text_drops_noise_tags():
    segments = [
        Segment(0, 1, "[Music]"),
        Segment(1, 2, "Real content here"),
        Segment(2, 3, "(laughs) more content"),
    ]
    text = segments_to_text(segments)
    assert "[Music]" not in text
    assert "Real content here" in text
    assert "more content" in text


def test_merge_segments_coalesces_into_windows():
    segments = [Segment(i * 5.0, i * 5.0 + 5.0, f"line {i}") for i in range(12)]
    merged = merge_segments(segments, window_seconds=30.0)
    assert len(merged) < len(segments)
    assert merged[0].start == 0.0
    assert merged[-1].end == 60.0
    assert all(s.text.strip() for s in merged)


def test_merge_segments_handles_an_empty_list():
    assert merge_segments([], 30.0) == []


def test_timestamp_formatting():
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(59) == "0:59"
    assert format_timestamp(75) == "1:15"
    assert format_timestamp(3725) == "1:02:05"


def test_slugify():
    assert slugify("SUCI Ep 212 - Mixed Use in Pilsen!") == "suci-ep-212-mixed-use-in-pilsen"
