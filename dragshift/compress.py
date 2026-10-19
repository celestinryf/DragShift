"""Compress by percentage for PDFs, video and audio: a small window that offers the usual
one-click settings ("Recommended") or a target share of the original size.

Images have their own Compress window (tools.CompressWindow) with a live preview; its
Percentage mode reuses the target-size search there."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QLabel, QWidget

from . import imaging
from .convert import save_bytes
from .tools import ToolWindow, form_grid, percent_row, toggle_row
from .util import file_size, human_size, sibling

# image resolution and JPEG quality steps tried from gentle to strong until the PDF fits
PDF_LADDER = [(200, 85), (150, 75), (120, 65), (96, 55), (72, 45), (50, 35)]


def compress_pdf_percent(src: str | Path, percent: int) -> Path:
    """The gentlest setting that makes the PDF at most `percent`% of its size, or the smallest
    one tried when none gets there (a PDF that's mostly text can't shrink much)."""
    goal = file_size(src) * percent / 100
    best: bytes | None = None
    for dpi, quality in PDF_LADDER:
        data = imaging.compressed_pdf_bytes(src, dpi, quality)
        if best is None or len(data) < len(best):
            best = data
        if len(data) <= goal:
            break
    out = sibling(src, "Compressed", ".pdf")
    save_bytes(out, best)
    return out


class PercentCompressWindow(ToolWindow):
    """Recommended settings, or "make it about N% of its size". `recommended(path)` and
    `by_percent(path, percent)` each write one compressed copy and return its path."""
    tool_name = "Compress"

    def __init__(self, paths, notify, recommended: Callable[[Path], Path],
                 by_percent: Callable[[Path, int], Path], blurb: str, note: str = ""):
        super().__init__("Compress", paths, notify, size=(460, 330))
        self.recommended, self.by_percent = recommended, by_percent
        self.total = sum(file_size(p) for p in self.paths)

        g = self.inspector.group("Compression", collapsible=False)
        row, self.mode = toggle_row(["Recommended", "Percentage"])
        g.addLayout(row)
        self.blurb = QLabel(blurb)
        self.blurb.setObjectName("dim")
        self.blurb.setWordWrap(True)
        g.addWidget(self.blurb)

        self.pct_box = QWidget()
        grid = form_grid()
        grid.setContentsMargins(0, 0, 0, 0)
        self.pct_box.setLayout(grid)
        self.pct = percent_row(grid, 0)
        self.estimate = QLabel()
        self.estimate.setObjectName("big")
        grid.addWidget(self.estimate, 1, 0, 1, 3)
        if note:
            hint = QLabel(note)
            hint.setObjectName("dim")
            hint.setWordWrap(True)
            grid.addWidget(hint, 2, 0, 1, 3)
        self.pct_box.setVisible(False)
        g.addWidget(self.pct_box)

        label = "Compress" if len(self.paths) == 1 else f"Compress {len(self.paths)} Files"
        self.finish(label, self.save)
        self.mode.idClicked.connect(self._mode)
        self.pct.valueChanged.connect(self._estimate)
        self._estimate()

    def _mode(self, i: int):
        self.blurb.setVisible(i == 0)
        self.pct_box.setVisible(i == 1)

    def _estimate(self):
        self.estimate.setText(f"{human_size(self.total)}  →  about {human_size(self.total * self.pct.value() / 100)}")

    def percent(self) -> int | None:
        return self.pct.value() if self.mode.checkedId() == 1 else None

    def save(self):
        paths, pct = list(self.paths), self.percent()
        if pct is None:
            fn = self.recommended
            self.run(lambda: [fn(p) for p in paths], "Compressing…")
        else:
            fn = self.by_percent
            self.run(lambda: [fn(p, pct) for p in paths], "Compressing…", f"aimed for {pct}%")


def pdf_window(paths, notify) -> PercentCompressWindow:
    return PercentCompressWindow(paths, notify, imaging.compress_pdf, compress_pdf_percent,
                                 "Pictures at 150 dpi, JPEG quality 70, and a tidy file structure.",
                                 "Pictures shrink until the PDF fits. Text barely compresses, so a "
                                 "mostly text PDF may stay bigger.")


def video_window(paths, notify) -> PercentCompressWindow:
    from . import media
    return PercentCompressWindow(paths, notify, media.compress, media.compress,
                                 "Same resolution at a quality that's usually about half the size.",
                                 "Video sizes land within about 10% of the aim.")


def audio_window(paths, notify) -> PercentCompressWindow:
    from . import media
    return PercentCompressWindow(paths, notify, media.compress_audio, media.compress_audio,
                                 "About half the bitrate (64 to 128 kbps). Lossless files become AAC.",
                                 "Sets the bitrate to this share of the original's (32 to 320 kbps).")
