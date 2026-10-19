"""Subtitles (dragshift/subtitles.py): SRT and VTT both ways, ASS/SSA in, TXT out. Every
conversion is checked against the exact expected file text."""
from pathlib import Path

import pytest

from dragshift import convert, report, subtitles
from dragshift.util import UserError, category

SRT = """1
00:00:01,000 --> 00:00:02,500
<i>Hello</i> &amp; <font color="#ff0000">welcome</font>

2
00:00:03,000 --> 00:00:04,000 X1:10 X2:20 Y1:30 Y2:40
{\\an8}Top line
second line

"""

VTT = """WEBVTT - a title

STYLE
::cue { color: yellow }

NOTE this is a comment
over two lines

intro
00:01.000 --> 00:02.500 align:start position:10%
<v Anna>Hi <b>there</b></v> &lt;3

00:00:03.000 --> 00:00:04.000
<c.yellow>Coloured</c> <00:00:03.500>karaoke &amp; more
"""


def write(tmp_path: Path, name: str, text: str, encoding="utf-8", newline="\n") -> Path:
    p = tmp_path / name
    p.write_bytes(text.replace("\n", newline).encode(encoding))
    return p


def out_text(outs) -> str:
    assert len(outs) == 1
    return outs[0].read_bytes().decode("utf-8")


# ---------------------------------------------------------------- basics
def test_category_and_targets():
    assert category("a.srt") == category("a.VTT") == category("a.ass") == category("a.ssa") == "subtitle"
    assert convert.targets_for("subtitle", ".srt") == ["VTT", "TXT"]
    assert convert.targets_for("subtitle", ".vtt") == ["SRT", "TXT"]
    assert convert.targets_for("subtitle", ".ass") == ["SRT", "VTT", "TXT"]


def test_every_target_has_a_menu_hint():
    from dragshift import app
    for ext in (".srt", ".vtt", ".ass"):
        hints = {**app.FORMAT_HINTS, **app.CATEGORY_FORMAT_HINTS.get("subtitle", {})}
        for t in convert.targets_for("subtitle", ext):
            assert hints.get(t), t
    assert app.CATEGORY_FORMAT_HINTS["subtitle"]["TXT"] == "Dialogue only"


@pytest.mark.parametrize("ms, srt, vtt", [
    (0, "00:00:00,000", "00:00:00.000"),
    (3_723_456, "01:02:03,456", "01:02:03.456"),
    (99 * 3_600_000 + 59 * 60_000 + 59_999, "99:59:59,999", "99:59:59.999"),
    (123 * 3_600_000 + 1, "123:00:00,001", "123:00:00.001"),   # hours keep growing past 99
    (-5, "00:00:00,000", "00:00:00.000"),
])
def test_fmt_time(ms, srt, vtt):
    assert subtitles.fmt_time(ms, ",") == srt
    assert subtitles.fmt_time(ms, ".") == vtt


@pytest.mark.parametrize("line, expected", [
    ("00:00:01,000 --> 00:00:02,000", (1000, 2000, "")),
    ("00:00:01.000 --> 00:00:02.000", (1000, 2000, "")),            # '.' in an SRT
    ("1:02.5 --> 1:03.25 align:end", (62_500, 63_250, "align:end")),  # VTT short form, short fractions
    ("100:00:00,000 --> 101:30:00,000", (360_000_000, 365_400_000, "")),
    ("0:00:01 --> 0:00:02", (1000, 2000, "")),                       # no fraction at all
    ("00:61:00,000 --> 00:62:00,000", None),                         # minutes > 59
    ("00:00:01,000 -> 00:00:02,000", None),
    ("garbage", None),
])
def test_parse_timing(line, expected):
    assert subtitles.parse_timing(line) == expected


# ---------------------------------------------------------------- SRT -> VTT / TXT
def test_srt_to_vtt_exact(tmp_path):
    src = write(tmp_path, "film.srt", SRT)
    outs = convert.convert(src, "subtitle", "VTT")
    assert outs[0].name == "film.vtt"
    assert out_text(outs) == (
        "WEBVTT\n"
        "\n"
        "00:00:01.000 --> 00:00:02.500\n"
        "<i>Hello</i> &amp; welcome\n"
        "\n"
        "00:00:03.000 --> 00:00:04.000 line:0\n"
        "Top line\n"
        "second line\n")


def test_srt_with_bom_crlf_and_no_numbers(tmp_path):
    text = "00:00:01,000 --> 00:00:02,000\nOne\n\n\n\n2\n00:00:02,000 --> 00:00:03,000\nTwo\n"
    src = write(tmp_path, "w.srt", "﻿" + text, newline="\r\n")
    assert out_text(subtitles.convert(src, "VTT")) == (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nOne\n\n00:00:02.000 --> 00:00:03.000\nTwo\n")


def test_srt_in_utf16_and_cp1252(tmp_path):
    src = write(tmp_path, "u16.srt", "﻿1\n00:00:01,000 --> 00:00:02,000\nÜber\n", encoding="utf-16-le")
    assert "Über" in out_text(subtitles.convert(src, "TXT"))
    src = write(tmp_path, "w1252.srt", "1\n00:00:01,000 --> 00:00:02,000\ncafé\n", encoding="cp1252")
    assert out_text(subtitles.convert(src, "TXT")) == "café\n"


def test_old_mac_line_endings(tmp_path):
    src = write(tmp_path, "cr.srt", "1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n00:00:03,000 --> 00:00:04,000\nB\n",
                newline="\r")
    assert out_text(subtitles.convert(src, "TXT")) == "A\nB\n"


def test_malformed_cues_are_skipped(tmp_path):
    text = ("1\n00:00:01,000 --> 00:00:02,000\nGood\n\n"
            "2\nnot a time line\nLost\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\n\n\n"             # no text
            "4\n00:99:00,000 --> 00:99:01,000\nBad minutes\n\n"
            "5\n00:00:06,000 --> 00:00:05,000\nEnds before it starts\n\n"
            "just some words\n\n"
            "6\n00:00:07,000 --> 00:00:08,000\n<b></b>\n\n"     # only tags
            "7\n00:00:09,000 --> 00:00:10,000\nAlso good\n")
    src = write(tmp_path, "m.srt", text)
    assert out_text(subtitles.convert(src, "VTT")) == (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\nGood\n\n"
        "00:00:06.000 --> 00:00:06.000\nEnds before it starts\n\n"
        "00:00:09.000 --> 00:00:10.000\nAlso good\n")


def test_hours_past_99_round_trip(tmp_path):
    src = write(tmp_path, "long.srt", "1\n123:04:05,006 --> 123:04:06,700\nLate\n")
    vtt = subtitles.convert(src, "VTT")[0]
    assert vtt.read_text() == "WEBVTT\n\n123:04:05.006 --> 123:04:06.700\nLate\n"
    back = subtitles.convert(vtt, "SRT")[0]
    assert back.name == "long (1).srt"
    assert back.read_text() == "1\n123:04:05,006 --> 123:04:06,700\nLate\n"


@pytest.mark.parametrize("an, settings", [(1, "line:100% align:start"), (2, ""), (5, "line:50%"),
                                          (9, "line:0 align:end")])
def test_srt_position_tags_become_cue_settings(tmp_path, an, settings):
    src = write(tmp_path, "p.srt", f"1\n00:00:01,000 --> 00:00:02,000\n{{\\an{an}}}Text\n")
    timing = "00:00:01.000 --> 00:00:02.000" + (f" {settings}" if settings else "")
    assert out_text(subtitles.convert(src, "VTT")) == f"WEBVTT\n\n{timing}\nText\n"


def test_vtt_escapes_stray_markup(tmp_path):
    src = write(tmp_path, "e.srt", "1\n00:00:01,000 --> 00:00:02,000\na < b & c > d --> e\n<u>kept</u>\n")
    assert out_text(subtitles.convert(src, "VTT")).splitlines()[-2:] == [
        "a &lt; b &amp; c &gt; d → e", "<u>kept</u>"]


# ---------------------------------------------------------------- VTT -> SRT / TXT
def test_vtt_to_srt_exact(tmp_path):
    src = write(tmp_path, "clip.vtt", VTT)
    outs = convert.convert(src, "subtitle", "SRT")
    assert outs[0].name == "clip.srt"
    assert out_text(outs) == (
        "1\n"
        "00:00:01,000 --> 00:00:02,500\n"
        "Anna: Hi <b>there</b> <3\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "Coloured karaoke & more\n")


def test_vtt_cue_settings_survive_vtt_round_trip(tmp_path):
    src = write(tmp_path, "s.vtt", VTT)
    cues = subtitles.read(src)
    assert cues[0].settings == "align:start position:10%"
    assert subtitles.to_vtt(cues).splitlines()[2] == "00:00:01.000 --> 00:00:02.500 align:start position:10%"


def test_vtt_without_header_is_a_user_error(tmp_path):
    src = write(tmp_path, "bad.vtt", "00:00:01.000 --> 00:00:02.000\nNo header\n")
    with pytest.raises(UserError, match="WEBVTT"):
        subtitles.convert(src, "SRT")


def test_file_without_any_cue_is_a_user_error(tmp_path):
    src = write(tmp_path, "empty.srt", "\n\njust text\n")
    with pytest.raises(UserError, match="No subtitles"):
        subtitles.convert(src, "VTT")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["empty.srt"]


def test_vtt_with_bom_and_crlf(tmp_path):
    src = write(tmp_path, "b.vtt", "﻿WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHi\n", newline="\r\n")
    assert out_text(subtitles.convert(src, "SRT")) == "1\n00:00:01,000 --> 00:00:02,000\nHi\n"


# ---------------------------------------------------------------- TXT
def test_txt_is_dialogue_only_with_repeats_removed(tmp_path):
    text = ("WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\n<i>Rolling</i> caption\n\n"
            "00:00:02.000 --> 00:00:03.000\nRolling caption\nnext   line\n\n"
            "00:00:03.000 --> 00:00:04.000\nnext line\n\n"
            "00:00:04.000 --> 00:00:05.000\n<v Bob>Final words</v>\n")
    src = write(tmp_path, "t.vtt", text)
    out = convert.convert(src, "subtitle", "TXT")
    assert out[0].name == "t.txt"
    assert out_text(out) == "Rolling caption\nnext line\nBob: Final words\n"


def test_srt_to_txt(tmp_path):
    src = write(tmp_path, "film.srt", SRT)
    assert out_text(subtitles.convert(src, "TXT")) == "Hello & welcome\nTop line\nsecond line\n"


# ---------------------------------------------------------------- ASS / SSA
ASS = """[Script Info]
Title: test

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:05.50,0:00:07.00,Default,,0,0,0,,Second, with commas
Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,not shown
Dialogue: 0,0:00:01.00,0:00:02.25,Default,,0,0,0,,{\\i1}First{\\i0} line\\Nbreak\\hspace
Dialogue: 0,bad,0:00:03.00,Default,,0,0,0,,broken time
Dialogue: 0,10:00:00.05,10:00:01.00,Default,,0,0,0,,{\\pos(10,20)\\b1}Late{\\b0}
"""


def test_ass_to_srt_exact(tmp_path):
    src = write(tmp_path, "anime.ass", ASS)
    assert out_text(convert.convert(src, "subtitle", "SRT")) == (
        "1\n00:00:01,000 --> 00:00:02,250\n<i>First</i> line\nbreak space\n\n"
        "2\n00:00:05,500 --> 00:00:07,000\nSecond, with commas\n\n"
        "3\n10:00:00,050 --> 10:00:01,000\n<b>Late</b>\n")


def test_ssa_to_vtt_and_txt(tmp_path):
    ssa = ASS.replace("[V4+ Styles]", "[V4 Styles]")
    src = write(tmp_path, "old.ssa", ssa)
    vtt = out_text(subtitles.convert(src, "VTT"))
    assert vtt.startswith("WEBVTT\n\n00:00:01.000 --> 00:00:02.250\n<i>First</i> line\nbreak space\n")
    assert out_text(subtitles.convert(src, "TXT")) == "First line\nbreak space\nSecond, with commas\nLate\n"


def test_ass_without_events_is_a_user_error(tmp_path):
    src = write(tmp_path, "x.ass", "[Script Info]\nTitle: nothing\n")
    with pytest.raises(UserError):
        subtitles.convert(src, "SRT")


# ---------------------------------------------------------------- safety and report
def test_never_overwrites_and_writes_lf_utf8(tmp_path):
    src = write(tmp_path, "a.srt", "1\n00:00:01,000 --> 00:00:02,000\nÄ\n", newline="\r\n")
    (tmp_path / "a.vtt").write_text("mine")
    out = subtitles.convert(src, "VTT")[0]
    assert out.name == "a (1).vtt" and (tmp_path / "a.vtt").read_text() == "mine"
    assert b"\r" not in out.read_bytes() and "Ä" in out.read_text(encoding="utf-8")


def test_unknown_target_raises():
    with pytest.raises(ValueError):
        subtitles.convert("x.srt", "DOCX")


def test_report_mentions_cue_count(tmp_path):
    src = write(tmp_path, "film.srt", SRT)
    out = subtitles.convert(src, "VTT")
    r = report.summarize("convert:VTT", [src], out)
    assert r.title == "SRT → VTT"
    assert "2 subtitles" in r.detail and "0:04" in r.detail


def test_long_file_name_converts(tmp_path):
    """A 250-character name: the result (same length) fits, and so must the temp file."""
    src = tmp_path / ("a" * 246 + ".srt")
    src.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    out = subtitles.convert(src, "VTT")[0]
    assert out.name == "a" * 246 + ".vtt" and "Hi" in out.read_text(encoding="utf-8")
