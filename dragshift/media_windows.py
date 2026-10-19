"""Video and audio tool windows: Trim, Crop, Speed and Frame.

They follow the image windows in tools.py: a canvas on the left (a preview frame, or a
waveform for sound) over a QuickTime-style timeline, and the Pages inspector on the right.
Preview frames, filmstrips and waveforms come from ffmpeg on worker threads (`Loader`), so
scrubbing never blocks the UI; saving goes through the app's background jobs (progress and
cancel on the notification card)."""
from __future__ import annotations

import itertools
import math
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, QPointF, QRectF, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen, QShortcut, QValidator
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import controls, design, media
from .tools import CropInspector, CropView, ToolWindow, crop_box, form_grid, slider, slider_row, toggle_row
from .util import UserError
from .window import ImageView, paint_canvas

PREVIEW_SIDE = 960       # longest side of preview frames; sharp on a Retina canvas, quick to decode
STRIP_THUMBS = 10        # frames in the timeline's filmstrip
STRIP_HEIGHT = 96        # filmstrip frame height in pixels (the bar is 48 pt tall at 2x)
WAVE_SIZE = (1600, 240)  # waveform image, stretched to the bar
SPEEDS = [0.25, 0.5, 0.75, 1.25, 1.5, 2.0, 4.0]
SPEED_STEP = 0.05        # the speed slider snaps to 1.25x, 1.3x... rather than 1.2968x


# ---------------------------------------------------------------- time text
def fmt_time(seconds: float) -> str:
    """0:05.3, 12:00.0, 1:02:03.5: tenths of a second, hours only when there are any."""
    if not math.isfinite(seconds):
        seconds = 0.0
    tenths = int(round(max(0.0, seconds) * 10))
    s, t = divmod(tenths, 10)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}.{t}" if h else f"{m}:{s:02d}.{t}"


_SECONDS = re.compile(r"\d+(?:[.,]\d*)?|[.,]\d+")
_WHOLE = re.compile(r"\d+")


def parse_time(text: str) -> float:
    """Seconds from typed text: 5, 5.5, 1:05, 1:05.3 or 1:02:03.5 (a decimal comma works too).
    Raises ValueError for anything else."""
    parts = text.strip().split(":")
    if (len(parts) > 3 or not _SECONDS.fullmatch(parts[-1])
            or not all(_WHOLE.fullmatch(p) for p in parts[:-1])):
        raise ValueError(f"Not a time: {text!r}")
    secs = float(parts[-1].replace(",", "."))
    whole = [int(p) for p in parts[:-1]]
    # only the first field may run past 59 ("90" seconds, "75:00" minutes)
    if (whole and secs >= 60) or any(v >= 60 for v in whole[1:]):
        raise ValueError(f"Not a time: {text!r}")
    return secs + sum(v * 60 ** (i + 1) for i, v in enumerate(reversed(whole)))


# ---------------------------------------------------------------- speed and crop maths
def speed_label(factor: float) -> str:
    return f"{factor:g}×"


def slider_to_speed(v: int) -> float:
    """The speed slider is logarithmic (-200..200 = 0.25x..4x, 0 = 1x), so 0.5x and 2x sit the
    same distance from the middle."""
    lo, hi = media.SPEED_RANGE
    f = round(2 ** (v / 100) / SPEED_STEP) * SPEED_STEP
    return round(min(hi, max(lo, f)), 2)


def speed_to_slider(factor: float) -> int:
    return round(100 * math.log2(factor))


def sped_duration(duration: float, factor: float) -> float:
    return duration / factor


def preview_size(display: tuple[int, int], max_side: int = PREVIEW_SIDE) -> tuple[int, int]:
    """Preview frame size for a video of `display` size (never upscaled)."""
    w, h = display
    s = min(1.0, max_side / max(w, h, 1))
    return max(2, round(w * s)), max(2, round(h * s))


def video_crop_rect(sel: QRectF, k: float, display: tuple[int, int],
                    ratio: float | None) -> tuple[int, int, int, int]:
    """A crop selected on the preview as (x, y, width, height) in display pixels (after
    rotation, which is what `media.crop` takes). Everything is rounded down to even numbers,
    since 4:2:0 video (H.264) can't have odd sizes, and kept inside the frame."""
    fw, fh = display
    x, y, w, h = crop_box(sel, k, display, ratio)
    x, y = max(0, x - x % 2), max(0, y - y % 2)
    w, h = min(w, fw - x), min(h, fh - y)
    return x, y, max(2, w - w % 2), max(2, h - h % 2)


# ---------------------------------------------------------------- background previews
class _Cache:
    """A small least-recently-used cache shared by every window."""

    def __init__(self, limit: int):
        self.limit = limit
        self.items: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.limit:
                self.items.popitem(last=False)


FRAMES = _Cache(240)     # PNG bytes keyed by file + frame number (tens to hundreds of KB each)
STRIPS = _Cache(16)
WAVES = _Cache(16)


def file_stamp(path: Path) -> tuple:
    """Cache key part that changes when the file does."""
    try:
        st = os.stat(path)
        return str(path), st.st_mtime_ns, st.st_size
    except OSError:
        return str(path), 0, 0


# Threading rule: a worker thread never holds a reference to a Qt object. If a worker held the
# last reference, Python would destroy that object on the worker thread (when the worker's
# frame ends, or when a garbage collection happens to run there), and Qt objects must die on the
# thread that owns them: that crashed the test suite now and then. So a task is plain data plus a
# function of plain data; it finds its Loader through an id, and results reach the UI thread
# through one bridge object that lives for the whole run.
class _Bridge(QObject):
    done = Signal(object)


_BRIDGE: _Bridge | None = None
_LOADERS: dict[int, "Loader"] = {}   # open loaders by id (UI thread only)
_IN_FLIGHT: set = set()              # tasks started and not yet delivered (UI thread only)
_ids = itertools.count(1)


def _bridge() -> _Bridge:
    """Created on the UI thread by the first request, then never freed."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = _Bridge()
        _BRIDGE.done.connect(_deliver)
    return _BRIDGE


class _Task:
    """What a worker thread sees: no Qt objects, only ids, keys, a function and its result."""

    def __init__(self, loader_id: int, kind: str, key, fn: Callable, bridge: _Bridge):
        self.loader_id, self.kind, self.key, self.fn = loader_id, kind, key, fn
        self.result = None
        self.emit = bridge.done.emit   # the bridge lives forever, so this is never its last reference

    def run(self):
        try:
            self.result = self.fn()
        except media.Cancelled:
            pass   # its window closed (Loader.close); nobody is waiting for this preview
        except Exception as e:  # noqa: BLE001  (a preview that fails just isn't shown)
            media._log(f"preview ({self.kind}) failed: {e}")
        self.fn = None   # drop the closure here, before the result travels back
        self.emit(self)


def _deliver(task: _Task):
    """Runs on the UI thread. The window may have closed while ffmpeg was working."""
    _IN_FLIGHT.discard(task)
    loader = _LOADERS.get(task.loader_id)
    if loader is not None and shiboken6.isValid(loader):
        loader._finished(task)


def drain(timeout: float = 30.0):
    """Block until every preview task has finished and been delivered, from every window (tests,
    screenshots). Workers hold no Qt objects, so the app itself never needs to wait."""
    deadline = time.monotonic() + timeout
    while _IN_FLIGHT and time.monotonic() < deadline:
        QThreadPool.globalInstance().waitForDone(50)
        QCoreApplication.processEvents()


class Loader(QObject):
    """Runs preview work off the UI thread: one job per kind at a time, and while one runs only
    the newest request waits (scrubbing asks for many frames; only the latest matters)."""
    loaded = Signal(str, object, object)   # kind, key, result (None if it failed)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.stop = threading.Event()      # set on close; long jobs check it between steps
        self.id = next(_ids)
        _LOADERS[self.id] = self
        self._busy: set[str] = set()
        self._next: dict[str, tuple] = {}
        self.destroyed.connect(lambda *_, i=self.id: _LOADERS.pop(i, None))

    def close(self):
        """Stop asking for previews; results still on their way are dropped. Setting `stop`
        also kills ffmpeg for jobs that take it (the waveform, the filmstrip between frames)."""
        self.stop.set()
        self._next.clear()
        _LOADERS.pop(self.id, None)

    def request(self, kind: str, key, fn: Callable):
        if self.stop.is_set():
            return
        if kind in self._busy:
            self._next[kind] = (key, fn)
            return
        self._busy.add(kind)
        task = _Task(self.id, kind, key, fn, _bridge())
        _IN_FLIGHT.add(task)
        QThreadPool.globalInstance().start(task.run)

    def _finished(self, task: _Task):
        self._busy.discard(task.kind)
        nxt = self._next.pop(task.kind, None)
        if nxt is not None:
            self.request(task.kind, *nxt)
        self.loaded.emit(task.kind, task.key, task.result)

    def busy(self) -> bool:
        return bool(self._busy)

    def wait(self, timeout: float = 30.0):
        """Block until this loader's requests have been delivered (tests and screenshots)."""
        deadline = time.monotonic() + timeout
        while self._busy and _LOADERS.get(self.id) is self and time.monotonic() < deadline:
            QThreadPool.globalInstance().waitForDone(50)
            QCoreApplication.processEvents()


def filmstrip(path: Path, info: media.Info, stop: threading.Event,
              count: int = STRIP_THUMBS, height: int = STRIP_HEIGHT) -> list[bytes | None]:
    """Evenly spaced thumbnails (keyframes, which is fast even on long files)."""
    w, h = info.size or (16, 9)
    size = (max(2, round(height * w / h / 2) * 2), height)
    step = info.duration / count if info.duration else 0.0
    thumbs: list[bytes | None] = []
    for i in range(count):
        if stop.is_set():
            break
        try:
            thumbs.append(media.preview_frame(path, (i + 0.5) * step, size, info.video[0].index, exact=False))
        except UserError:
            thumbs.append(None)
    return thumbs


def tinted(img: QImage, color: QColor) -> QImage:
    """A white-on-transparent image (the waveform) recoloured."""
    out = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(out.rect(), color)
    p.end()
    return out


# ---------------------------------------------------------------- widgets
class TimeField(QDoubleSpinBox):
    """A time entry with a stepper. Shows 0:05.3 / 1:02:03.5 and accepts 5, 5.5, 1:05..."""

    def __init__(self, duration: float, step: float = 0.1, name: str = ""):
        super().__init__()
        self.setDecimals(3)
        self.setRange(0.0, max(0.0, duration))
        self.setSingleStep(step)
        self.setKeyboardTracking(False)
        self.setAlignment(Qt.AlignRight)
        self.setCorrectionMode(QAbstractSpinBox.CorrectToPreviousValue)
        self.setAccessibleName(name)

    def textFromValue(self, v: float) -> str:
        return fmt_time(v)

    def valueFromText(self, text: str) -> float:
        try:
            return parse_time(text)
        except ValueError:
            return self.value()

    def validate(self, text: str, pos: int):
        try:
            v = parse_time(text)
        except ValueError:
            return QValidator.Intermediate, text, pos
        ok = self.minimum() <= v <= self.maximum()
        return (QValidator.Acceptable if ok else QValidator.Intermediate), text, pos


class TimelineBar(QWidget):
    """QuickTime's trim bar: a filmstrip (video) or waveform (sound) with a play head, and in
    trim mode Photos-style yellow handles for the in and out points.

    Emits `scrubbed(t)` when the user moves the play head (dragging a handle moves it too, so
    the preview shows the frame at the cut) and `trimmed(start, end)` when a handle moves."""
    scrubbed = Signal(float)
    trimmed = Signal(float, float)
    HANDLE = 12          # handle width in points
    BORDER = 3           # yellow border above and below the selection
    RADIUS = 6

    def __init__(self, duration: float, fps: float = 0.0, trim: bool = False, tall: bool = False, parent=None):
        super().__init__(parent)
        self.duration = max(duration, 0.001)
        self.step = 1 / fps if fps else 0.1          # arrow keys move one frame
        self.min_len = max(2 * media.MIN_TRIM, self.step)
        self.trim = trim
        self.lo, self.hi, self.t = 0.0, self.duration, 0.0
        self.strip: list[QImage | None] = []
        self.wave: QImage | None = None
        self._wave_tinted: QImage | None = None
        self._drag: str | None = None
        self._keyboard_focus = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("Timeline")
        if tall:
            self.setMinimumHeight(96)
            self.setMaximumHeight(220)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        else:
            self.setFixedHeight(56)
        design.theme().changed.connect(self._retint)

    # ---- state (setters don't emit; only the user's actions do)
    def set_playhead(self, t: float):
        self.t = min(max(t, 0.0), self.duration)
        self.setAccessibleDescription(fmt_time(self.t))
        self.update()

    def set_range(self, lo: float, hi: float):
        self.lo, self.hi = max(0.0, lo), min(self.duration, hi)
        self.update()

    def set_strip(self, images: list[QImage | None]):
        self.strip = images
        self.update()

    def set_wave(self, img: QImage | None):
        self.wave = img
        self._retint()

    def _retint(self):
        self._wave_tinted = tinted(self.wave, design.color("label", alpha=0.75)) if self.wave else None
        self.update()

    # ---- geometry
    def track(self) -> QRectF:
        return QRectF(self.HANDLE, 4, max(1, self.width() - 2 * self.HANDLE), max(1, self.height() - 8))

    def x_of(self, t: float) -> float:
        tr = self.track()
        return tr.left() + tr.width() * t / self.duration

    def time_at(self, x: float) -> float:
        tr = self.track()
        return min(max((x - tr.left()) / tr.width() * self.duration, 0.0), self.duration)

    def _hit(self, x: float) -> str:
        if self.trim:
            xl, xh = self.x_of(self.lo), self.x_of(self.hi)
            on_lo = xl - self.HANDLE - 2 <= x <= xl + 3
            on_hi = xh - 3 <= x <= xh + self.HANDLE + 2
            if on_lo and on_hi:   # a tiny selection: take the nearer handle
                return "lo" if abs(x - xl) < abs(x - xh) else "hi"
            if on_lo:
                return "lo"
            if on_hi:
                return "hi"
        return "scrub"

    # ---- interaction
    def focusInEvent(self, e):
        # like AppKit, the focus ring is for keyboard navigation, not for clicks or opening
        self._keyboard_focus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._keyboard_focus = False
        super().focusOutEvent(e)

    def _move_to(self, mode: str, t: float):
        if mode == "lo":
            self.lo = max(0.0, min(t, self.hi - self.min_len))
            self.t = self.lo
            self.trimmed.emit(self.lo, self.hi)
        elif mode == "hi":
            self.hi = min(self.duration, max(t, self.lo + self.min_len))
            self.t = self.hi
            self.trimmed.emit(self.lo, self.hi)
        else:
            self.t = min(max(t, self.lo), self.hi) if self.trim else t
        self.set_playhead(self.t)
        self.scrubbed.emit(self.t)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._drag = self._hit(e.position().x())
        self._move_to(self._drag, self.time_at(e.position().x()))

    def mouseMoveEvent(self, e):
        if self._drag:
            self._move_to(self._drag, self.time_at(e.position().x()))
        else:
            self.setCursor(Qt.SizeHorCursor if self._hit(e.position().x()) != "scrub" else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        step = 1.0 if e.modifiers() & Qt.ShiftModifier else self.step
        lo, hi = (self.lo, self.hi) if self.trim else (0.0, self.duration)
        moves = {Qt.Key_Left: self.t - step, Qt.Key_Right: self.t + step, Qt.Key_Home: lo, Qt.Key_End: hi}
        if e.key() in moves:
            self._move_to("scrub", min(max(moves[e.key()], 0.0), self.duration))
        else:
            super().keyPressEvent(e)

    # ---- painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        tr = self.track()
        clip = QPainterPath()
        clip.addRoundedRect(tr, self.RADIUS, self.RADIUS)
        p.save()
        p.setClipPath(clip)
        p.fillRect(tr, design.color("label", alpha=0.1))
        if self.strip:
            slot = tr.width() / len(self.strip)
            for i, img in enumerate(self.strip):
                if img is not None and not img.isNull():
                    self._draw_filled(p, img, QRectF(tr.left() + i * slot, tr.top(), slot + 0.5, tr.height()))
        elif self._wave_tinted is not None:
            p.drawImage(tr.adjusted(0, 4, 0, -4), self._wave_tinted, QRectF(self._wave_tinted.rect()))
        if self.trim:   # dim what will be cut away
            shade = QColor(0, 0, 0, 130)
            p.fillRect(QRectF(tr.left(), tr.top(), self.x_of(self.lo) - tr.left(), tr.height()), shade)
            p.fillRect(QRectF(self.x_of(self.hi), tr.top(), tr.right() - self.x_of(self.hi), tr.height()), shade)
        p.restore()
        if self.trim:
            self._paint_handles(p, tr)
        else:
            p.setPen(QPen(design.color("separator", alpha=1.5), design.HAIRLINE))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(tr.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        self._paint_playhead(p)
        if self.hasFocus() and self._keyboard_focus:
            controls.focus_ring(p, tr.adjusted(-self.HANDLE if self.trim else 0, 0,
                                               self.HANDLE if self.trim else 0, 0), self.RADIUS)

    @staticmethod
    def _draw_filled(p: QPainter, img: QImage, target: QRectF):
        """Draw `img` filling `target`, cropping the overflow (aspect fill)."""
        s = max(target.width() / img.width(), target.height() / img.height())
        w, h = target.width() / s, target.height() / s
        p.drawImage(target, img, QRectF((img.width() - w) / 2, (img.height() - h) / 2, w, h))

    def _paint_handles(self, p: QPainter, tr: QRectF):
        xl, xh, b = self.x_of(self.lo), self.x_of(self.hi), self.BORDER
        outer = QPainterPath()
        outer.addRoundedRect(QRectF(xl - self.HANDLE, tr.top(), xh - xl + 2 * self.HANDLE, tr.height()),
                             self.RADIUS, self.RADIUS)
        hole = QPainterPath()
        hole.addRect(QRectF(xl, tr.top() + b, xh - xl, tr.height() - 2 * b))
        p.fillPath(outer.subtracted(hole), design.color("yellow"))
        p.setPen(QPen(QColor(0, 0, 0, 170), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        cy, arm = tr.center().y(), 4.0
        for cx, d in ((xl - self.HANDLE / 2, 1), (xh + self.HANDLE / 2, -1)):   # ‹ and ›
            p.drawPolyline([QPointF(cx + d * arm / 2, cy - arm), QPointF(cx - d * arm / 2, cy),
                            QPointF(cx + d * arm / 2, cy + arm)])

    def _paint_playhead(self, p: QPainter):
        x = self.x_of(self.t)
        p.setPen(QPen(QColor(0, 0, 0, 90), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x, 1.5), QPointF(x, self.height() - 1.5))
        p.setPen(QPen(QColor(255, 255, 255), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x, 1.5), QPointF(x, self.height() - 1.5))


class MediaCanvas(QWidget):
    """A media window's canvas: the preview (none for sound) over the timeline, with the play
    head's time on the left and a second figure (length, or the selection) on the right."""
    TIMES_H = 22

    def __init__(self, view: QWidget | None, bar: TimelineBar, parent=None):
        super().__init__(parent)
        self.corner_radius = 0.0   # set by the window to follow its rounded corner
        self.view, self.bar = view, bar
        self.left_text = self.right_text = ""
        m = ImageView.MARGIN
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, m - 8)
        lay.setSpacing(0)
        if view is not None:
            lay.addWidget(view, 1)
        else:
            lay.addStretch(1)
        row = QHBoxLayout()
        row.setContentsMargins(m - TimelineBar.HANDLE, 0, m - TimelineBar.HANDLE, 0)
        row.addWidget(bar)
        lay.addLayout(row, 0 if view is not None else 2)
        lay.addSpacing(self.TIMES_H)
        if view is None:
            lay.addStretch(1)
        self.time_font = design.font("callout", 500)
        try:
            self.time_font.setFeature(QFont.Tag("tnum"), 1)   # digits that don't jiggle while scrubbing
        except TypeError:
            # PySide6 before 6.8.3 can't make a Tag from a string, and Debian 12 on arm64 tops out at
            # 6.8.0.2: the times just use proportional digits there
            pass
        self.setMinimumSize(360, 240)

    def set_times(self, left: str, right: str):
        self.left_text, self.right_text = left, right
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        paint_canvas(p, QRectF(self.rect()), self.corner_radius)
        g = self.bar.geometry()
        r = QRectF(g.left() + TimelineBar.HANDLE, g.bottom() + 4, g.width() - 2 * TimelineBar.HANDLE,
                   self.TIMES_H - 4)
        p.setFont(self.time_font)
        p.setPen(design.color("secondaryLabel"))
        p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, self.left_text)
        p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, self.right_text)


# ---------------------------------------------------------------- windows
class MediaWindow(ToolWindow):
    """Shared by Trim, Crop and Frame: probes the file, shows the canvas and timeline, and keeps
    the preview frame in step with the play head."""

    def __init__(self, title: str, path, notify, size, trim: bool = False, needs_video: bool = False):
        super().__init__(title, path, notify, size=size, toolbar=True)
        self.info = media.probe(self.path)
        if needs_video:
            media._require_video(self.info)
        self.duration = self.info.duration
        self.stamp = file_stamp(self.path)
        self.loader = Loader(self)
        self.loader.loaded.connect(self._loaded)
        v = self.info.video[0] if self.info.video else None
        self.bar = TimelineBar(self.duration, v.fps if v else 0.0, trim=trim, tall=v is None)
        self.bar.scrubbed.connect(self.scrub)
        self.view: ImageView | None = None
        if v is not None:
            self.pv_size = preview_size(self.info.size)
            self.k = self.info.size[0] / self.pv_size[0]
            blank = QImage(*self.pv_size, QImage.Format_RGB32)
            blank.fill(QColor(0, 0, 0))
            self.view = self.make_view(blank)
        self.media_canvas = MediaCanvas(self.view, self.bar)
        self.set_canvas(self.media_canvas)
        self._want = 0.0
        self._frame_timer = QTimer(self, singleShot=True, interval=40, timeout=self._request_frame)
        self._load_timeline()

    def make_view(self, image: QImage) -> ImageView:
        return ImageView(image)

    def closeEvent(self, e):
        if hasattr(self, "_frame_timer"):   # absent if __init__ failed (the file can't be read)
            self.loader.close()
            self._frame_timer.stop()
        super().closeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self.bar.setFocus()   # arrow keys step frames right away

    # ---- play head and preview frame
    def go_to(self, t: float):
        """Move the play head (from code, a field or a toolbar button) and show that frame."""
        self.bar.set_playhead(t)
        self.scrub(self.bar.t)

    def scrub(self, t: float):
        self._want = t
        self.update_times()
        if self.view is None:
            return
        data = FRAMES.get(self._frame_key(t))
        if data is not None:
            self._show_frame(data)
        else:
            self._frame_timer.start()   # debounced: a drag asks for dozens of frames

    def _frame_at(self, t: float) -> float:
        """ffmpeg returns nothing for a seek right at the end, so stop a frame short."""
        end = max(0.0, self.duration - max(self.bar.step, 0.05))
        return min(max(0.0, t), end)

    def _frame_key(self, t: float) -> tuple:
        return (*self.stamp, self.pv_size, round(self._frame_at(t) / self.bar.step))

    def _request_frame(self):
        at, size, index, path = self._frame_at(self._want), self.pv_size, self.info.video[0].index, self.path
        self.loader.request("frame", self._frame_key(self._want),
                            lambda: media.preview_frame(path, at, size, index))

    def _show_frame(self, data: bytes):
        img = QImage.fromData(data)
        if not img.isNull():
            self.view.set_image(img)

    def _load_timeline(self):
        path, info, stop = self.path, self.info, self.loader.stop
        if info.video:
            key = (*self.stamp, "strip")
            cached = STRIPS.get(key)
            if cached is not None:
                self._loaded("strip", key, cached)
            else:
                self.loader.request("strip", key, lambda: filmstrip(path, info, stop))
        elif info.audio:
            key = (*self.stamp, "wave")
            cached = WAVES.get(key)
            if cached is not None:
                self._loaded("wave", key, cached)
            else:
                index = info.audio[0].index
                self.loader.request("wave", key, lambda: media.waveform(path, WAVE_SIZE, index, stop))

    def _loaded(self, kind: str, key, result):
        if result is None:
            return
        if kind == "frame":
            FRAMES.put(key, result)
            self._show_frame(result)
        elif kind == "strip":
            STRIPS.put(key, result)
            self.bar.set_strip([QImage.fromData(b) if b else None for b in result])
        elif kind == "wave":
            WAVES.put(key, result)
            self.bar.set_wave(QImage.fromData(result))

    # ---- labels
    def update_times(self):
        self.media_canvas.set_times(fmt_time(self.bar.t), self.right_time())

    def right_time(self) -> str:
        return fmt_time(self.duration)


class TrimWindow(MediaWindow):
    tool_name = "Trim"
    HINTS = ["Cuts exactly where you set it. Takes longer, because the file is re-encoded.",
             "Copies without re-encoding, so nothing is lost. Video can only start on a keyframe, "
             "so the cut may begin slightly early."]

    def __init__(self, path, notify):
        super().__init__("Trim", path, notify, size=(1040, 700), trim=True)
        self.toolbar.add(("arrow.right.to.line", "Set Start", "⇥", self.set_start),
                         ("arrow.left.to.line", "Set End", "⇤", self.set_end))
        self.toolbar.add(("arrow.counterclockwise", "Reset", "↺", self.reset))
        # Final Cut / QuickTime keys for the in and out points
        QShortcut(QKeySequence("I"), self, activated=self.set_start)
        QShortcut(QKeySequence("O"), self, activated=self.set_end)

        g = self.inspector.group("Range")
        grid = form_grid()
        self.start = TimeField(self.duration, self.bar.step, "Start")
        self.end = TimeField(self.duration, self.bar.step, "End")
        self.end.setValue(self.duration)
        self.length = QLabel()
        self.length.setObjectName("dim")
        for r, (label, w) in enumerate((("Start", self.start), ("End", self.end), ("Length", self.length))):
            grid.addWidget(controls.form_label(label), r, 0)
            grid.addWidget(w, r, 1)
        g.addLayout(grid)
        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        g.addWidget(self.warn)

        g = self.inspector.group("Cut")
        row, self.mode = toggle_row(["Precise", "Fast"])
        g.addLayout(row)
        self.hint = QLabel(self.HINTS[0])
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        g.addWidget(self.hint)
        self.finish("Trim", self.apply)

        self.mode.idClicked.connect(lambda i: self.hint.setText(self.HINTS[i]))
        self.bar.trimmed.connect(self._from_bar)
        self.start.valueChanged.connect(lambda v: self._from_fields(v))
        self.end.valueChanged.connect(lambda v: self._from_fields(v))
        self.refresh()
        self.go_to(0.0)

    def _from_bar(self, lo: float, hi: float):
        for box, v in ((self.start, lo), (self.end, hi)):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)
        self.refresh()

    def _from_fields(self, moved_to: float):
        lo, hi = self.start.value(), self.end.value()
        if hi - lo >= self.bar.min_len:
            self.bar.set_range(lo, hi)
            self.go_to(moved_to)   # show the frame at the point that was just typed
        self.refresh()

    def selection(self) -> tuple[float, float]:
        return self.start.value(), self.end.value()

    def refresh(self):
        lo, hi = self.selection()
        self.length.setText(fmt_time(max(0.0, hi - lo)))
        try:
            media.trim_range(lo, hi, self.duration)
            self.warn.hide()
            ok = True
        except UserError as e:
            self.warn.setText(str(e))
            self.warn.show()
            ok = False
        self.primary_btn.setEnabled(ok)
        self.update_times()

    def right_time(self) -> str:
        lo, hi = self.selection() if hasattr(self, "end") else (0.0, self.duration)
        return f"{fmt_time(max(0.0, hi - lo))} selected"

    def set_start(self):
        self.start.setValue(min(self.bar.t, self.end.value() - self.bar.min_len))

    def set_end(self):
        self.end.setValue(max(self.bar.t, self.start.value() + self.bar.min_len))

    def reset(self):
        self.start.setValue(0.0)
        self.end.setValue(self.duration)
        self.mode.button(0).click()
        self.go_to(0.0)

    def apply(self):
        (lo, hi), fast, path = self.selection(), self.mode.checkedId() == 1, self.path
        extra = f"{fmt_time(lo)} to {fmt_time(hi)}" + (" · no re-encode" if fast else "")
        self.run(lambda: [media.trim(path, lo, hi, fast=fast)], "Trimming…", extra)


class FrameWindow(MediaWindow):
    """Pick a frame on the timeline, then save it as a PNG at full size."""
    tool_name = "Frame"

    def __init__(self, path, notify):
        super().__init__("Save Frame", path, notify, size=(980, 660), needs_video=True)
        self.toolbar.add(("backward.frame", "Previous", "◀", lambda: self.go_to(self.bar.t - self.bar.step)),
                         ("forward.frame", "Next", "▶", lambda: self.go_to(self.bar.t + self.bar.step)))

        g = self.inspector.group("Frame")
        grid = form_grid()
        self.time = TimeField(self.duration, self.bar.step, "Time")
        grid.addWidget(controls.form_label("Time"), 0, 0)
        grid.addWidget(self.time, 0, 1)
        g.addLayout(grid)
        w, h = self.info.size
        out = QLabel(f"PNG  ·  {w} × {h} px")
        out.setObjectName("dim")
        g.addWidget(out)
        tip = QLabel("Drag along the timeline, or use the arrow keys to step one frame.")
        tip.setObjectName("dim")
        tip.setWordWrap(True)
        g.addWidget(tip)
        self.finish("Save Frame", self.apply)

        self.time.valueChanged.connect(self.go_to)
        self.go_to(self.duration / 2)

    def scrub(self, t: float):
        super().scrub(t)
        self.time.blockSignals(True)
        self.time.setValue(t)
        self.time.blockSignals(False)

    def apply(self):
        at, path = self._frame_at(self.bar.t), self.path
        self.run(lambda: [media.frame_grab(path, at)], "Saving the frame…", f"at {fmt_time(at)}")


class VideoCropWindow(CropInspector, MediaWindow):
    tool_name = "Crop"
    RATIOS = [("Free", None), ("Original", "orig"), ("1:1", 1.0), ("4:3", 4 / 3), ("16:9", 16 / 9),
              ("9:16", 9 / 16)]

    def __init__(self, path, notify):
        super().__init__("Crop Video", path, notify, size=(1040, 720), needs_video=True)
        self.toolbar.add(("arrow.counterclockwise", "Reset", "↺", self.reset))
        self.crop_inspector(step=2, tip="Double-click the video to reset. Sizes are rounded to even "
                                        "numbers, which video needs.")
        self.finish("Crop", self.apply)
        self.go_to(self.duration / 2)

    def make_view(self, image: QImage) -> ImageView:
        view = CropView(image, self.k)
        view.even = True
        return view

    def reset(self):
        self.reset_ratio()
        self.view.reset_sel()

    def crop_rect(self) -> tuple[int, int, int, int]:
        return video_crop_rect(self.view.sel, self.view.k, self.info.size, self.view.ratio)

    def apply(self):
        rect, path = self.crop_rect(), self.path
        self.run(lambda: [media.crop(path, rect)], "Cropping…")


class SpeedWindow(ToolWindow):
    """Speed has nothing to point at, so it's a compact window like Resize: a preset pop-up
    with a slider, a pitch option for the sound, and the resulting length."""
    tool_name = "Speed"
    DEFAULT = 2.0

    def __init__(self, path, notify):
        super().__init__("Change Speed", path, notify, size=(560, 420))
        self.info = media.probe(self.path)

        g = self.inspector.group("Speed", collapsible=False)
        grid = form_grid()
        self.preset = QComboBox()
        self.preset.setAccessibleName("Preset")
        self.preset.addItems([speed_label(f) for f in SPEEDS] + ["Custom"])
        grid.addWidget(controls.form_label("Preset"), 0, 0)
        grid.addWidget(self.preset, 0, 1, 1, 2, Qt.AlignLeft)
        lo, hi = media.SPEED_RANGE
        self.slider = slider(speed_to_slider(lo), speed_to_slider(hi), speed_to_slider(self.DEFAULT))
        slider_row(grid, 1, "Speed", self.slider, lambda v: speed_label(slider_to_speed(v)))
        g.addLayout(grid)

        self.pitch: QCheckBox | None = None
        if self.info.audio:
            g = self.inspector.group("Sound", collapsible=False)
            self.pitch = QCheckBox("Keep pitch")
            self.pitch.setChecked(True)
            g.addWidget(self.pitch)
            self.pitch_note = QLabel()
            self.pitch_note.setObjectName("dim")
            self.pitch_note.setWordWrap(True)
            g.addWidget(self.pitch_note)
            self.pitch.toggled.connect(lambda _: self.refresh())

        g = self.inspector.group("Result", collapsible=False)
        self.result = QLabel()
        self.result.setObjectName("big")
        self.result_note = QLabel()
        self.result_note.setObjectName("dim")
        g.addWidget(self.result)
        g.addWidget(self.result_note)
        self.finish("Change Speed", self.apply)

        self.preset.currentIndexChanged.connect(self._from_preset)
        self.slider.valueChanged.connect(lambda _: self.refresh())
        self.refresh()

    def factor(self) -> float:
        return slider_to_speed(self.slider.value())

    def _from_preset(self, i: int):
        if i < len(SPEEDS):
            self.slider.setValue(speed_to_slider(SPEEDS[i]))

    def refresh(self):
        f = self.factor()
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(SPEEDS.index(f) if f in SPEEDS else len(SPEEDS))
        self.preset.blockSignals(False)
        d = self.info.duration
        self.result.setText(f"{fmt_time(d)}  →  {fmt_time(sped_duration(d, f))}")
        if f == 1:
            self.result_note.setText("Normal speed: pick a faster or slower one.")
        else:
            self.result_note.setText(f"{speed_label(f)} {'faster' if f > 1 else 'slower'} · same format")
        if self.pitch is not None:
            self.pitch_note.setText("Voices and music sound the same, just faster or slower."
                                    if self.pitch.isChecked() else
                                    "Like a tape played at another speed: higher when faster, lower when slower.")
        self.primary_btn.setEnabled(f != 1)

    def apply(self):
        f, path = self.factor(), self.path
        keep_pitch = self.pitch is None or self.pitch.isChecked()
        extra = speed_label(f) + ("" if keep_pitch else " · pitch changed")
        self.run(lambda: [media.speed(path, f, keep_pitch)], "Changing speed…", extra)
