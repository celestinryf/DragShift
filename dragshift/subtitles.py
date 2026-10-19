"""Subtitles: SRT and WebVTT both ways, ASS/SSA in, and plain-text transcripts out.

Everything goes through a list of `Cue`s (start and end in milliseconds, text with only <b>,
<i> and <u> kept) plus an optional position for VTT cue settings. Readers are forgiving: a
byte-order mark, Windows or old-Mac line endings, a missing cue number, '.' instead of ','
and hours past 99 are all fine, and a cue with an unreadable time line is skipped instead of
failing the whole file."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from .convert import read_text, save_text
from .util import SUBTITLE_EXTS, UserError, plural, sibling

TARGETS = {"srt": ["VTT", "TXT"], "vtt": ["SRT", "TXT"], "ass": ["SRT", "VTT", "TXT"], "ssa": ["SRT", "VTT", "TXT"]}

# 00:01:02,345 / 1:02.345 / 123:00:00,5 (the fraction is a decimal fraction of a second)
TIME = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.](\d+))?"
TIMING = re.compile(rf"^\s*{TIME}\s*-->\s*{TIME}(.*)$")
ASS_TIME = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")
KEEP_TAGS = re.compile(r"</?([biu])\s*>", re.I)
# a tag starts with a letter or '/' (<i>, </c>, <v Anna>) or is a time stamp (<00:00:01.000>);
# "a < b" stays text
ANY_TAG = re.compile(r"</?(?:[A-Za-z][^<>\n]*|\d[\d:.]*)>")
VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>", re.I)
ASS_OVERRIDE = re.compile(r"\{[^}]*\}")
SRT_POSITION = re.compile(r"\{\\an(\d)\}")
# {\an1}..{\an9} is a numpad layout (7 8 9 top, 1 2 3 bottom); VTT says it with cue settings
AN_SETTINGS = {1: "line:100% align:start", 2: "", 3: "line:100% align:end",
               4: "line:50% align:start", 5: "line:50%", 6: "line:50% align:end",
               7: "line:0 align:start", 8: "line:0", 9: "line:0 align:end"}


@dataclass
class Cue:
    start: int          # milliseconds
    end: int
    text: str           # lines joined by "\n"; only <b>, <i>, <u> tags left
    settings: str = ""  # WebVTT cue settings ("line:0 align:start"), empty for none


def targets_for(src_ext: str) -> list[str]:
    return list(TARGETS.get(src_ext.lower().lstrip("."), []))


# ---------------------------------------------------------------- time stamps
def _ms(h, m, s, frac) -> int:
    frac_ms = int((frac or "0")[:3].ljust(3, "0"))
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + frac_ms


def fmt_time(ms: int, sep: str) -> str:
    """'01:02:03,456' (SRT, sep=',') or '01:02:03.456' (VTT). Hours keep growing past 99."""
    ms = max(0, ms)
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, f = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:03d}"


def parse_timing(line: str) -> tuple[int, int, str] | None:
    """(start, end, whatever follows: VTT cue settings or SRT coordinates) or None."""
    m = TIMING.match(line)
    if not m:
        return None
    g = m.groups()
    if int(g[1]) > 59 or int(g[2]) > 59 or int(g[5]) > 59 or int(g[6]) > 59:
        return None
    return _ms(*g[0:4]), _ms(*g[4:8]), g[8].strip()


# ---------------------------------------------------------------- readers
def _lines(src: str | Path) -> list[str]:
    text = read_text(src).lstrip("\ufeff")        # read_text drops a UTF-8 BOM; UTF-16 keeps one
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _blocks(lines: list[str]) -> list[list[str]]:
    blocks, cur = [], []
    for line in lines:
        if line.strip():
            cur.append(line.rstrip())
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def _clean_markup(text: str) -> str:
    """Keep <b>, <i>, <u>; a VTT voice tag becomes 'Name: '; every other tag goes (<font>,
    <c.yellow>, <00:00:01.000>, <ruby>…). Entities are decoded."""
    text = VOICE.sub(lambda m: f"{m.group(1).strip()}: " if m.group(1).strip() else "", text)
    keep: list[str] = []

    def stash(m):
        keep.append(f"<{'/' if m.group(0)[1] == '/' else ''}{m.group(1).lower()}>")
        return f"\x00{len(keep) - 1}\x00"

    text = KEEP_TAGS.sub(stash, text)
    text = ANY_TAG.sub("", text)
    text = html.unescape(text).replace("\u00a0", " ").replace("\u200e", "").replace("\u200f", "")
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def _cue_from(block: list[str], timing_at: int, settings_from_srt: bool) -> Cue | None:
    parsed = parse_timing(block[timing_at])
    if parsed is None:
        return None
    start, end, after = parsed
    raw = "\n".join(l.strip() for l in block[timing_at + 1:])
    settings = ""
    if settings_from_srt:
        pos = SRT_POSITION.search(raw)
        if pos:
            settings = AN_SETTINGS.get(int(pos.group(1)), "")
        raw = ASS_OVERRIDE.sub("", raw)
    else:
        settings = " ".join(after.split())
    text = _clean_markup(raw).strip()
    if not ANY_TAG.sub("", text).strip():
        return None
    return Cue(start, max(start, end), text, settings)


def read_srt(src: str | Path) -> list[Cue]:
    cues = []
    for block in _blocks(_lines(src)):
        # the cue number is optional in practice; the time line is the first one with "-->"
        at = next((i for i, l in enumerate(block[:2]) if "-->" in l), None)
        if at is None:
            continue
        cue = _cue_from(block, at, settings_from_srt=True)
        if cue:
            cues.append(cue)
    return cues


def read_vtt(src: str | Path) -> list[Cue]:
    lines = _lines(src)
    if not lines or not lines[0].strip().startswith("WEBVTT"):
        raise UserError("This doesn't look like a WebVTT file (it should start with “WEBVTT”).")
    cues = []
    for block in _blocks(lines[1:]):
        head = block[0].strip()
        if head.startswith(("NOTE", "STYLE", "REGION")) and "-->" not in head:
            continue
        # an optional cue identifier comes before the time line
        at = next((i for i, l in enumerate(block[:2]) if "-->" in l), None)
        if at is None:
            continue
        cue = _cue_from(block, at, settings_from_srt=False)
        if cue:
            cues.append(cue)
    return cues


def _ass_ms(text: str) -> int | None:
    m = ASS_TIME.match(text.strip())
    return _ms(*m.groups()) if m else None


def _ass_text(text: str) -> str:
    """ASS overrides: {\\i1}…{\\i0} become <i>…</i> (same for b, u); the rest are dropped.
    \\N and \\n are line breaks, \\h a hard space."""
    def override(m):
        out = []
        for tag, on in re.findall(r"\\([biu])(\d)", m.group(0)):
            out.append(f"<{tag}>" if on != "0" else f"</{tag}>")
        return "".join(out)

    text = ASS_OVERRIDE.sub(override, text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return "\n".join(l.strip() for l in text.split("\n") if l.strip())


def read_ass(src: str | Path) -> list[Cue]:
    fields: list[str] = []
    in_events = False
    cues = []
    for line in _lines(src):
        s = line.strip()
        if s.startswith("["):
            in_events = s.lower() == "[events]"
            continue
        if not in_events:
            continue
        key, _, rest = s.partition(":")
        if key.lower() == "format":
            fields = [f.strip().lower() for f in rest.split(",")]
        elif key.lower() == "dialogue" and fields:
            # Text is the last field and may itself contain commas
            values = [v.strip() for v in rest.split(",", len(fields) - 1)]
            row = dict(zip(fields, values))
            start, end = _ass_ms(row.get("start", "")), _ass_ms(row.get("end", ""))
            text = _ass_text(row.get("text", ""))
            if start is None or end is None or not ANY_TAG.sub("", text).strip():
                continue
            cues.append(Cue(start, max(start, end), _clean_markup(text)))
    cues.sort(key=lambda c: (c.start, c.end))   # ASS events can be in any order; SRT/VTT can't
    return cues


def read(src: str | Path) -> list[Cue]:
    ext = Path(src).suffix.lower()
    cues = {".srt": read_srt, ".vtt": read_vtt, ".ass": read_ass, ".ssa": read_ass}[ext](src)
    if not cues:
        raise UserError("No subtitles were found in this file.")
    return cues


# ---------------------------------------------------------------- writers
def to_srt(cues: list[Cue]) -> str:
    blocks = [f"{i}\n{fmt_time(c.start, ',')} --> {fmt_time(c.end, ',')}\n{c.text}"
              for i, c in enumerate(cues, 1)]
    return "\n\n".join(blocks) + "\n"


def _vtt_escape(text: str) -> str:
    """'&' and stray '<' must be escaped in WebVTT; <b>/<i>/<u> stay tags. '-->' can't appear
    in cue text at all."""
    parts = re.split(r"(</?[biu]>)", text)
    out = [p if KEEP_TAGS.fullmatch(p) else p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
           for p in parts]
    return "".join(out).replace("--&gt;", "→")


def to_vtt(cues: list[Cue]) -> str:
    blocks = ["WEBVTT"]
    for c in cues:
        timing = f"{fmt_time(c.start, '.')} --> {fmt_time(c.end, '.')}"
        if c.settings:
            timing += " " + c.settings
        text = "\n".join(l for l in _vtt_escape(c.text).split("\n") if l.strip())
        blocks.append(f"{timing}\n{text}")
    return "\n\n".join(blocks) + "\n"


def to_txt(cues: list[Cue]) -> str:
    """Dialogue only: no numbers, times or tags, one subtitle line per line, and a line that
    repeats the one before it (roll-up captions) written once."""
    out: list[str] = []
    for c in cues:
        for line in ANY_TAG.sub("", c.text).split("\n"):
            line = " ".join(line.split())
            if line and (not out or out[-1] != line):
                out.append(line)
    return "\n".join(out) + "\n"


WRITERS = {"SRT": (to_srt, ".srt"), "VTT": (to_vtt, ".vtt"), "TXT": (to_txt, ".txt")}


def convert(src: str | Path, target: str) -> list[Path]:
    target = target.upper()
    if target not in WRITERS:
        raise ValueError(f"Can't convert subtitles to {target}")
    write, ext = WRITERS[target]
    text = write(read(src))
    out = sibling(src, ext=ext)
    save_text(out, text, newline="\n")
    return [out]


def facts(path: str | Path) -> list[str]:
    """['412 subtitles', '1:32:05'] for the report card."""
    if Path(path).suffix.lower() not in SUBTITLE_EXTS:
        return []
    try:
        cues = read(path)
    except (UserError, OSError):
        return []
    from .media import fmt_duration
    return [plural(len(cues), "subtitle"), fmt_duration(max(c.end for c in cues) / 1000)]
