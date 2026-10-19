"""Shared helpers: output paths, file categories, sizes."""
from __future__ import annotations

import errno
import os
import re
from contextlib import contextmanager
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".tif", ".tiff",
              ".avif", ".bmp", ".gif", ".ico"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
HTML_EXTS = (".html", ".htm")
TEXT_EXTS = {".txt", ".log", ".json", ".xml", *HTML_EXTS}
SHEET_EXTS = {".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv"}
SLIDES_EXTS = {".pptx", ".ppt"}
EPUB_EXTS = {".epub"}
MARKDOWN_EXTS = {".md", ".markdown"}
RTF_EXTS = {".rtf", ".rtfd"}       # .rtfd is a folder bundle (TextEdit with pictures)
PAGES_EXTS = {".pages"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp",
              ".ts", ".mts"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aiff", ".aif", ".wma",
              ".alac", ".caf"}
SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa"}
SVG_EXTS = {".svg"}
# the last suffix only: "a.tar.gz" is ".gz" here; archives.archive_suffix tells the full one
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".tbz", ".xz", ".txz", ".rar"}

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # old Office files, and encrypted new ones
# control characters XML can't store (so XLSX, ODS, PPTX and the HTML lxml parses)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
NAME_TOO_LONG = "This file's name is too long to save the result. Give it a shorter name, then try again."


def category(path: str | Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOCX_EXTS:
        return "docx"
    if ext in SHEET_EXTS:
        return "sheet"
    if ext in SLIDES_EXTS:
        return "slides"
    if ext in EPUB_EXTS:
        return "epub"
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in RTF_EXTS:
        return "rtf"
    if ext in PAGES_EXTS:
        return "pages"
    if ext in TEXT_EXTS:
        return "text"
    if ext in VIDEO_EXTS:
        return "video" if ext != ".ts" or _is_mpeg_ts(path) else "text"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext in SVG_EXTS:
        return "svg"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return None


def _is_mpeg_ts(path: str | Path) -> bool:
    """.ts is also TypeScript. MPEG transport streams are 188-byte packets that each start with
    the sync byte 0x47, so check the first two packets."""
    try:
        with open(path, "rb") as f:
            head = f.read(189)
    except OSError:
        return False
    return len(head) == 189 and head[0] == 0x47 and head[188] == 0x47


def unique_path(path: str | Path) -> Path:
    """Return `path`, or `name (1).ext`, `name (2).ext`... if it already exists."""
    p = Path(path)
    with readable_name_errors():
        if not p.exists():
            return p
        i = 1
        while True:
            cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
            if not cand.exists():
                return cand
            i += 1


class UserError(Exception):
    """An error with a message that's fine to show the user as-is."""


@contextmanager
def readable_name_errors():
    """A result name past the file system's limit (255 bytes on most) as a UserError, not a raw
    OSError. Windows reports a too-long path as error 206."""
    try:
        yield
    except OSError as e:
        if e.errno == errno.ENAMETOOLONG or getattr(e, "winerror", None) == 206:
            raise UserError(NAME_TOO_LONG) from e
        raise


def encrypted_ole(path: str | Path) -> bool:
    """A password-protected DOCX, XLSX or PPTX: Office keeps it inside an OLE container with an
    EncryptionInfo stream, instead of the usual zip."""
    try:
        with open(path, "rb") as f:
            if f.read(len(OLE_MAGIC)) != OLE_MAGIC:
                return False
            return "EncryptionInfo".encode("utf-16-le") in f.read()
    except OSError:
        return False


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 file', '2 files'; pass `many` for irregular words ('1 child', '3 children')."""
    if n == 1:
        return f"{n} {word}"
    return f"{n} {many or word + 's'}"


# Runtime options (filled from Settings at start-up; plain values so worker threads can read them)
CONFIG = {
    "output": "next",        # "next" = beside the original, "folder" = a "DragShift" subfolder
    "jpg_quality": 90,
    "pdf_dpi": 200,
}


def output_dir(src: str | Path) -> Path:
    d = Path(src).parent
    if CONFIG.get("output") == "folder":
        d = d / "DragShift"
        d.mkdir(exist_ok=True)
    return d


def sibling(src: str | Path, suffix_label: str = "", ext: str | None = None) -> Path:
    """Output path for `src`. sibling('a.jpg', 'Cropped') -> 'a Cropped.jpg' (never overwrites)."""
    s = output_dir(src) / Path(src).name
    stem = f"{s.stem} {suffix_label}".strip() if suffix_label else s.stem
    new_ext = ext if ext is not None else s.suffix
    if not new_ext.startswith("."):
        new_ext = "." + new_ext
    return unique_path(s.with_name(stem + new_ext))


def human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def file_size(path: str | Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------- background job lifetime
# QThreadPool runs a QRunnable in C++, but the Python object (and the QObject carrying its
# signals) can be garbage-collected while it's still running: results then never arrive,
# or the app crashes. Keep every job alive until its result has been handled.
#
# The other half: `release` runs on the UI thread when the result arrives, but the worker can
# still be returning from run() and then holds the last reference. The job would then be freed
# on the worker thread, taking its `sig` QObject with it, and Qt objects must be destroyed on
# their own thread (this crashed the tests now and then). So `keep` hands `sig` to a UI-thread
# owner and `release` deletes it there with deleteLater.
ALIVE: set = set()
_SIG_OWNER = None


def _sig_owner():
    global _SIG_OWNER
    if _SIG_OWNER is None:
        from PySide6.QtCore import QObject
        _SIG_OWNER = QObject()
    return _SIG_OWNER


def keep(job):
    """Call on the UI thread, before starting the job."""
    job.setAutoDelete(False)
    sig = getattr(job, "sig", None)
    if sig is not None:
        sig.setParent(_sig_owner())
    ALIVE.add(job)
    return job


def release(job):
    """Call on the UI thread, once the job's result has been handled."""
    if job in ALIVE:
        sig = getattr(job, "sig", None)
        if sig is not None:
            sig.deleteLater()
    ALIVE.discard(job)
