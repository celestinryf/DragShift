"""Turns 'what just happened' into a short, useful summary for the notification:
sizes saved, new dimensions, pages, fields removed…"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import category, file_size, human_size, plural


@dataclass
class Result:
    title: str
    detail: str
    outs: list[Path]
    srcs: list[Path] = field(default_factory=list)
    good: str = ""     # highlighted figure, e.g. "−75%"
    undo: Callable[[], str] | None = None   # custom Undo (returns what it did); default trashes `outs`

    @property
    def thumb(self) -> Path | None:
        for o in self.outs:
            if category(o) == "image":
                return o
        return None


def media_info(path: Path):
    try:
        from . import media
        return media.probe(path)
    except Exception:
        return None


def media_facts(path: Path) -> list[str]:
    """['1920×1080', '0:42'] for a video, ['3:07'] for audio, [] if ffprobe can't read it."""
    info = media_info(path)
    if info is None:
        return []
    from .media import fmt_duration
    return ([_dim_str(info.size)] if info.size else []) + ([fmt_duration(info.duration)] if info.duration else [])


def dims(path: Path) -> tuple[int, int] | None:
    if category(path) in ("video", "audio"):
        info = media_info(path)
        return info.size if info else None
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def pages(path: Path) -> int | None:
    try:
        from .convert import fitz
        with fitz.open(path) as d:
            return d.page_count
    except Exception:
        return None


def source_units(src: Path) -> tuple[int | None, str]:
    """(3, 'sheets') for a workbook, (12, 'slides') for a deck, else its page count."""
    try:
        from .documents import unit_count
        found = unit_count(src)
    except Exception:
        found = None
    return found if found else (pages(src), "pages")


def document_facts(path: Path) -> list[str]:
    """['3 sheets', '120 rows'] for a spreadsheet, ['12 slides'] for a deck."""
    try:
        from .documents import facts
        return facts(path)
    except Exception:
        return []


def subtitle_facts(path: Path) -> list[str]:
    """['412 subtitles', '1:32:05'] for a subtitle file."""
    try:
        from .subtitles import facts
        return facts(path)
    except Exception:
        return []


def file_count(paths: list[Path]) -> int:
    from .archives import count_files
    return count_files(paths)


def format_name(path: Path) -> str:
    """'TAR.GZ' for 'a.tar.gz', 'PNG' for 'a.png'."""
    if category(path) == "archive":
        from .archives import format_label
        return format_label(path)
    return path.suffix.lstrip(".").upper()


def size_change(before: int, after: int) -> tuple[str, str]:
    """('2.4 MB → 610 KB', '75% smaller') — second part empty if no meaningful change."""
    text = f"{human_size(before)} → {human_size(after)}"
    if before <= 0:
        return text, ""
    pct = (1 - after / before) * 100
    if pct >= 1:
        return text, f"{pct:.0f}% smaller"
    if pct <= -1:
        return text, f"{-pct:.0f}% larger"
    return text, "same size"


def _dim_str(d) -> str:
    return f"{d[0]}×{d[1]}" if d else ""


MEDIA_TITLES = {"Audio": "Audio extracted", "Mute": "Sound removed", "Frame": "Frame saved",
                "Normalize": "Volume evened out", "Strip Tags": "Tags removed", "Trim": "Trimmed",
                "Speed": "Speed changed"}


def summarize(action: str, srcs: list, outs: list, extra: str = "") -> Result:
    srcs = [Path(s) for s in srcs]
    outs = [Path(o) for o in outs if o]
    if not outs:
        return Result(action, extra or "Nothing was written", [], srcs)
    src = srcs[0] if srcs else outs[0]
    before = sum(file_size(s) for s in srcs if s.is_file())
    after = sum(file_size(o) for o in outs if o.is_file())
    kind, _, name = action.partition(":")
    o0 = outs[0]
    good = ""

    if kind == "convert":
        fmt = name
        if len(srcs) > 1 and len(outs) == 1:
            title = f"{len(srcs)} files → 1 {fmt}"
        elif len(outs) > 1 and len(srcs) == 1:
            n, unit = source_units(src)
            title = f"{n or len(outs)} {unit} → {len(outs)} {fmt}s"
        elif len(outs) > 1:
            title = f"{len(outs)} files → {fmt}"
        else:
            title = f"{format_name(src)} → {fmt}"
        bits = [o0.name if len(outs) == 1 else f"{o0.parent.name}/{o0.name} …"]
        d = dims(o0) if category(o0) == "image" else None
        if d:
            bits.append(_dim_str(d))
        if category(o0) in ("video", "audio"):
            bits += media_facts(o0)
        if o0.suffix.lower() == ".pdf":
            n = pages(o0)
            if n:
                bits.append(plural(n, "page"))
        if len(outs) == 1:
            bits += document_facts(o0)
        if category(o0) == "subtitle":
            bits += subtitle_facts(o0)
        bits.append(human_size(after))
        return Result(title, " · ".join(bits), outs, srcs)

    if kind == "archive":   # a new archive of the whole selection
        title = f"Compressed “{src.name}”" if len(srcs) == 1 else f"Compressed {len(srcs)} items"
        return Result(title, f"{o0.name} · {plural(file_count(srcs), 'file')} · {human_size(after)}", outs, srcs)

    if kind == "action":   # Copy / Move: `extra` is the destination folder's name
        verb = {"Copy": "Copied", "Move": "Moved"}.get(name, name)
        n = len(outs)
        title = f"{verb} {n} items" if n > 1 else f"{verb} “{o0.name}”"
        return Result(title, f"To {extra}" if extra else str(o0.parent), outs, srcs)

    if name == "Compress":
        sizes, verdict = size_change(before, after)
        title = f"Compressed {len(srcs)} files" if len(srcs) > 1 else "Compressed"
        if verdict.endswith("larger") or verdict == "same size":
            verdict = "already well compressed"
        return Result(title, sizes + (f" · {extra}" if extra else ""), outs, srcs, good=verdict)

    if name in ("Crop", "Resize", "Edit"):
        d0, d1 = dims(src), dims(o0)
        verb = {"Crop": "Cropped", "Resize": "Resized", "Edit": "Edited"}[name]
        if len(outs) > 1:
            return Result(f"{verb} {len(outs)} images", f"{human_size(before)} → {human_size(after)}", outs, srcs)
        detail = f"{_dim_str(d0)} → {_dim_str(d1)}" if d0 and d1 and d0 != d1 else _dim_str(d1)
        if extra:
            detail += f" · {extra}"
        return Result(verb, f"{detail} · {o0.name}", outs, srcs)

    if name == "Add BG":
        return Result("Background added", f"{_dim_str(dims(o0))} PNG · {human_size(after)} · {o0.name}", outs, srcs)

    if name == "Annotate":
        return Result("Annotated", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs)

    if name == "Redact":
        return Result("Redacted", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs,
                      good="burned into pixels")

    if name == "Metadata":
        return Result("Metadata removed", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs,
                      good="private copy")

    if name == "Extract Files":
        n = file_count(outs)
        title = f"Extracted {plural(n, 'file')}" if len(srcs) == 1 else f"Extracted {len(srcs)} archives"
        shown = f"{o0.name}/" if o0.is_dir() else o0.name
        return Result(title, shown if len(outs) == 1 else f"{shown} …", outs, srcs)

    if name == "Reorder":
        n = pages(o0)
        bits = [plural(n, "page"), *([extra] if extra else []), o0.name]
        return Result("Pages reordered", " · ".join(bits), outs, srcs)

    if name == "Split":
        return Result(f"Split into {len(outs)} PDFs", f"{o0.name} …", outs, srcs)

    if name == "Merge":
        n = pages(o0)
        return Result(f"Merged {len(srcs)} PDFs", f"{n} pages · {human_size(after)} · {o0.name}", outs, srcs)

    if name in MEDIA_TITLES:
        title = MEDIA_TITLES[name] + (f" · {len(outs)} files" if len(outs) > 1 else "")
        good = "private copy" if name == "Strip Tags" else ""
        bits = [*media_facts(o0), *([extra] if extra else []), human_size(after), o0.name]
        return Result(title, " · ".join(bits), outs, srcs, good=good)

    if name == "Rotate":
        if category(o0) == "video":
            return Result("Rotated 90°", " · ".join([*media_facts(o0), o0.name]), outs, srcs)
        n = pages(o0)
        return Result("Rotated 90°", f"{plural(n, 'page')} · {o0.name}", outs, srcs)

    if name == "Protect":
        return Result("Password protected", f"AES-256 · {o0.name}", outs, srcs, good="encrypted")

    if name == "Extract":
        return Result(f"Extracted {plural(len(outs), 'image')}",
                      f"{o0.parent.name}/ · {human_size(after)}", outs, srcs)

    # generic
    return Result(name or action, f"{o0.name} · {human_size(after)}", outs, srcs)
