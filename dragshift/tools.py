"""Tool windows: Compress, Resize, Crop, Edit, Annotate, Redact, Add BG, Metadata, Protect.

Every window opens on a fast, reduced-size preview. Saving runs on the full-resolution
image in the background (the window closes immediately; a notification reports the result)."""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QImage, QKeySequence,
                           QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import controls, design, imaging
from .convert import open_image, open_preview
from .glass import pil_from_qimage, qimage_from_pil
from .util import file_size, human_size, plural, sibling
from .window import GlassWindow, ImageView

Notify = Callable[..., None]
PERCENT_RANGE = (10, 90)   # "compress to N% of the original" (images here, PDFs and media in compress.py)
PERCENT_DEFAULT = 50


# ---------------------------------------------------------------- small widgets
def section(text: str) -> QLabel:
    """A small bold section header (Pages' inspector style, sentence case)."""
    l = QLabel(text)
    l.setObjectName("section")
    return l


def slider(lo: int, hi: int, val: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def toggle_row(labels: list[str], checked: int = 0, small: bool = True) -> tuple[QHBoxLayout, QButtonGroup]:
    """A segmented control (one choice) in a row; callers may add buttons after it."""
    row = QHBoxLayout()
    row.setSpacing(design.SPACING["s"])
    seg = controls.Segmented(labels, checked, small=small)
    row.addWidget(seg)
    row.addStretch(1)
    return row, seg.group


def value_box(s: QSlider, suffix: str) -> QSpinBox:
    """A numeric field with stepper kept in sync with a slider (Pages pairs every slider
    with one)."""
    box = QSpinBox()
    box.setRange(s.minimum(), s.maximum())
    box.setSuffix(suffix)
    box.setValue(s.value())
    box.setFixedWidth(68)
    box.setAlignment(Qt.AlignRight)
    box.setAccessibleName(s.accessibleName())
    box.valueChanged.connect(s.setValue)
    s.valueChanged.connect(box.setValue)
    return box


def slider_row(grid: QGridLayout, r: int, label: str, s: QSlider, fmt: Callable[[int], str],
               suffix: str | None = None):
    """Label, slider and its value: a stepper field when `suffix` is given, else a label."""
    grid.addWidget(controls.form_label(label), r, 0)
    s.setAccessibleName(label)
    grid.addWidget(s, r, 1)
    if suffix is not None:
        val = value_box(s, suffix)
    else:
        val = QLabel(fmt(s.value()))
        val.setObjectName("dim")
        val.setMinimumWidth(48)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        s.valueChanged.connect(lambda v: val.setText(fmt(v)))
    grid.addWidget(val, r, 2)
    return val


def percent_row(grid: QGridLayout, r: int) -> QSlider:
    """The "Size of original" slider every compress-by-percentage window shares."""
    s = slider(*PERCENT_RANGE, PERCENT_DEFAULT)
    s.setSingleStep(5)
    s.setPageStep(10)
    slider_row(grid, r, "Size of original", s, lambda v: f"{v}%", "%")
    return s


def form_grid() -> QGridLayout:
    g = QGridLayout()
    g.setHorizontalSpacing(design.SPACING["s"])
    g.setVerticalSpacing(design.SPACING["s"])
    g.setColumnStretch(1, 1)
    return g


def size_fields(w: QSpinBox, h: QSpinBox, unit: str = "px") -> QGridLayout:
    """Width / Height steppers side by side with small captions under them (Pages' Arrange)."""
    g = QGridLayout()
    g.setHorizontalSpacing(design.SPACING["s"])
    g.setVerticalSpacing(design.SPACING["xxs"])
    for col, (box, cap) in enumerate(((w, "Width"), (h, "Height"))):
        box.setSuffix(f" {unit}")
        box.setAccessibleName(cap)
        g.addWidget(box, 0, col)
        c = QLabel(cap)
        c.setObjectName("dim")
        c.setAlignment(Qt.AlignHCenter)
        g.addWidget(c, 1, col)
    return g


def dims_text(w: int, h: int) -> str:
    return f"{w} × {h}"


class ToolWindow(GlassWindow):
    """A tool: optional canvas on the left, Pages-style inspector on the right (full width
    when there's no canvas), Cancel / primary at the bottom of the inspector, and actions
    in a unified toolbar. `self.body` is the inspector's first page, so code written for a
    plain column still works."""
    tool_name = ""

    def __init__(self, title: str, paths, notify: Notify, size=(820, 680), toolbar: bool = False,
                 tabs: list[str] | None = None):
        self.paths = [Path(p) for p in (paths if isinstance(paths, (list, tuple)) else [paths])]
        self.path = self.paths[0]
        if len(self.paths) == 1:
            sub = f"{self.path.name}  ·  {human_size(file_size(self.path))}"
        else:
            total = sum(file_size(p) for p in self.paths)
            sub = f"{len(self.paths)} files  ·  {human_size(total)}"
        super().__init__(title, size=size, toolbar=toolbar, subtitle=sub)
        self.notify = notify
        self.submit = None   # set by the app: run work in the background with a spinner
        self.subtitle = QLabel(sub)   # kept for callers; the visible subtitle is in the title bar
        self.subtitle.hide()
        self.content.removeItem(self.body)
        self.split = QHBoxLayout()
        self.split.setContentsMargins(0, 0, 0, 0)
        self.split.setSpacing(0)
        self.content.addLayout(self.split, 1)
        self.inspector = controls.Inspector(tabs)
        self.split.addWidget(self.inspector, 1)
        self.body = self.inspector.content(0)
        self.canvas: QWidget | None = None
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self._enter)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self._enter)
        self.primary_btn: QPushButton | None = None

    def set_canvas(self, w: QWidget):
        """Put the document canvas left of the inspector, which becomes a fixed-width panel."""
        self.canvas = w
        if hasattr(w, "corner_radius") and self.native:
            w.corner_radius = design.RADIUS["window"]
        self.split.insertWidget(0, w, 1)
        self.split.insertWidget(1, controls.Hairline(vertical=True))
        self.split.setStretchFactor(self.inspector, 0)
        self.inspector.make_panel()

    def set_subtitle(self, text: str):
        self.subtitle.setText(text)
        super().set_subtitle(text)

    def _enter(self):
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit) and isinstance(fw.parent(), ImageView):
            fw.returnPressed.emit()   # Enter in an on-canvas text box finishes the text
            return
        if self.primary_btn is not None and self.primary_btn.isEnabled():
            self.primary_btn.click()

    def footer(self, primary_text: str, on_primary, on_reset=None, extra_buttons=()) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(design.SPACING["s"])
        if on_reset:
            reset = QPushButton("Reset")
            reset.clicked.connect(on_reset)
            row.addWidget(reset)
        for b in extra_buttons:
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)
        ok = QPushButton(primary_text)
        ok.setObjectName("primary")
        ok.setDefault(True)          # macOS draws the default button in the accent colour
        ok.clicked.connect(on_primary)
        row.addWidget(ok)
        self.primary_btn = ok
        return row

    def finish(self, primary_text: str, on_primary, on_reset=None, extra_buttons=()):
        """Close the inspector's groups and add the Cancel / primary footer under them."""
        self.inspector.finish()
        self.inspector.set_footer(self.footer(primary_text, on_primary, on_reset, extra_buttons))

    def run(self, fn: Callable[[], list], working: str, extra: str = ""):
        """Save in the background (window closes now) or, without the app, synchronously."""
        action = f"tool:{self.tool_name}"
        if self.submit is not None:
            self.submit(action, [str(p) for p in self.paths], fn, working, extra)
            self.close()
        else:
            outs = fn()
            self.notify(action, [str(p) for p in self.paths], outs, extra)
            self.close()


# ================================================================ CROP
class CropView(ImageView):
    HANDLE = 9

    def __init__(self, image: QImage, k: float):
        super().__init__(image)
        self.k = k
        self.even = False   # video: H.264 needs even dimensions
        self.ratio: float | None = None
        self.sel = QRectF(0, 0, image.width(), image.height())
        self._mode: str | None = None
        self._start = QPointF()
        self._orig = QRectF()

    def set_ratio(self, ratio: float | None):
        self.ratio = ratio
        if ratio:
            W, H = self.image.width(), self.image.height()
            w, h = W, W / ratio
            if h > H:
                h, w = H, H * ratio
            self.sel = QRectF((W - w) / 2, (H - h) / 2, w, h)
        self.update()
        self.changed.emit()

    def out_size(self) -> tuple[int, int]:
        """The selection's size in full-size pixels (rounded down to even numbers when `even`)."""
        w, h = round(self.sel.width() * self.k), round(self.sel.height() * self.k)
        return (w - w % 2, h - h % 2) if self.even else (w, h)

    def reset_sel(self):
        self.sel = QRectF(0, 0, self.image.width(), self.image.height())
        if self.ratio:
            self.set_ratio(self.ratio)
        self.update()
        self.changed.emit()

    def _hit(self, pos: QPointF) -> str | None:
        r = self.rect_to_widget(self.sel)
        h = self.HANDLE + 4
        near = lambda a, b: abs(a - b) <= h
        l, t, rt, b = near(pos.x(), r.left()), near(pos.y(), r.top()), near(pos.x(), r.right()), near(pos.y(), r.bottom())
        inside_x = r.left() - h <= pos.x() <= r.right() + h
        inside_y = r.top() - h <= pos.y() <= r.bottom() + h
        if t and l: return "tl"
        if t and rt: return "tr"
        if b and l: return "bl"
        if b and rt: return "br"
        if t and inside_x: return "t"
        if b and inside_x: return "b"
        if l and inside_y: return "l"
        if rt and inside_y: return "r"
        if r.contains(pos): return "move"
        return None

    CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
               "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}

    def mousePressEvent(self, e):
        self._mode = self._hit(e.position())
        if self._mode is None and self.target_rect().contains(e.position()):
            self._mode = "new"
        self._start = self.clamp_image(self.to_image(e.position()))
        self._orig = QRectF(self.sel)

    def mouseDoubleClickEvent(self, e):
        self.reset_sel()

    def mouseMoveEvent(self, e):
        if not self._mode:
            m = self._hit(e.position())
            self.setCursor(self.CURSORS.get(m, Qt.CrossCursor))
            return
        p = self.clamp_image(self.to_image(e.position()))
        W, H = self.image.width(), self.image.height()
        r = QRectF(self._orig)
        if self._mode == "move":
            d = p - self._start
            r.translate(d)
            r.moveLeft(min(max(r.left(), 0), W - r.width()))
            r.moveTop(min(max(r.top(), 0), H - r.height()))
        elif self._mode == "new":
            r = QRectF(self._start, p).normalized()
            if self.ratio and r.height() > 0:
                r.setHeight(r.width() / self.ratio)
        else:
            if "l" in self._mode: r.setLeft(p.x())
            if "r" in self._mode: r.setRight(p.x())
            if "t" in self._mode: r.setTop(p.y())
            if "b" in self._mode: r.setBottom(p.y())
            r = r.normalized()
            if self.ratio:
                if self._mode in ("t", "b"):
                    r.setWidth(r.height() * self.ratio)
                else:
                    nh = r.width() / self.ratio
                    if "t" in self._mode:
                        r.setTop(r.bottom() - nh)
                    else:
                        r.setHeight(nh)
        r = r.intersected(QRectF(0, 0, W, H))
        if r.width() >= 4 and r.height() >= 4:
            self.sel = r
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, e):
        self._mode = None

    def paint_overlay(self, p: QPainter, t: QRectF):
        r = self.rect_to_widget(self.sel)
        shade = QPainterPath()
        shade.addRect(t)
        hole = QPainterPath()
        hole.addRect(r)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 140))
        p.setPen(QPen(QColor(255, 255, 255, 230), 1))
        p.drawRect(r)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        for i in (1, 2):  # rule of thirds
            x = r.left() + r.width() * i / 3
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        # Photos-style corner brackets
        arm = min(18.0, r.width() / 3, r.height() / 3)
        p.setPen(QPen(QColor(255, 255, 255), 3.5, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        for pt, sx, sy in ((r.topLeft(), 1, 1), (r.topRight(), -1, 1), (r.bottomLeft(), 1, -1),
                           (r.bottomRight(), -1, -1)):
            p.drawPolyline(QPolygonF([QPointF(pt.x(), pt.y() + sy * arm), pt, QPointF(pt.x() + sx * arm, pt.y())]))
        # live output size badge (a dark HUD capsule, as in Photos)
        txt = dims_text(*self.out_size())
        p.setFont(design.font("callout", 500))
        w = p.fontMetrics().horizontalAdvance(txt) + 18
        badge = QRectF(r.center().x() - w / 2, r.bottom() - 30, w, 22)
        if badge.top() < r.top() + 4:
            badge.moveTop(r.bottom() + 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(badge, 11, 11)
        p.setPen(QColor(255, 255, 255))
        p.drawText(badge, Qt.AlignCenter, txt)


def crop_box(sel: QRectF, k: float, full: tuple[int, int], ratio: float | None) -> tuple[int, int, int, int]:
    """A preview selection as (x, y, width, height) in full-size pixels. Edges within 3 px of
    the image's edges snap to them (preview rounding), and a fixed ratio stays exact (no
    1600×1599)."""
    fw, fh = full
    x0, y0 = round(sel.left() * k), round(sel.top() * k)
    w, h = round(sel.width() * k), round(sel.height() * k)
    if x0 <= 3:
        w, x0 = w + x0, 0
    if y0 <= 3:
        h, y0 = h + y0, 0
    snap_w = abs(x0 + w - fw) <= 3
    snap_h = abs(y0 + h - fh) <= 3
    if snap_w:
        w = fw - x0
    if snap_h:
        h = fh - y0
    if ratio:
        if snap_h and not snap_w:
            w = round(h * ratio)
        else:
            h = round(w / ratio)
        if y0 + h > fh:
            h = fh - y0
            w = round(h * ratio)
        if x0 + w > fw:
            w = fw - x0
            h = round(w / ratio)
    return x0, y0, w, h


class CropInspector:
    """The aspect-ratio pop-up and Width / Height steppers shared by the image and video Crop
    windows. Needs `self.view` (a CropView) and `self.inspector`."""
    RATIOS = [("Free", None), ("Original", "orig"), ("1:1", 1.0), ("4:3", 4 / 3), ("3:2", 1.5),
              ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:5", 0.8)]

    def crop_inspector(self, step: int = 1, tip: str = "Double-click the image to reset"):
        g = self.inspector.group("Aspect Ratio")
        self.ratio_box = QComboBox()
        self.ratio_box.setAccessibleName("Aspect ratio")
        for n, _ in self.RATIOS:
            self.ratio_box.addItem(n)
        self.ratio_box.currentIndexChanged.connect(self._ratio)
        g.addWidget(self.ratio_box)

        g = self.inspector.group("Size")
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(step, 100000)
            s.setSingleStep(step)
        g.addLayout(size_fields(self.w, self.h))
        hint = QLabel(tip)
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        g.addWidget(hint)
        self.w.editingFinished.connect(self._from_spin)
        self.h.editingFinished.connect(self._from_spin)
        self.view.changed.connect(self._to_spin)
        self._to_spin()

    def _ratio(self, i):
        r = self.RATIOS[i][1]
        if r == "orig":
            r = self.view.image.width() / self.view.image.height()
        self.view.set_ratio(r)

    def _to_spin(self):
        for box, v in zip((self.w, self.h), self.view.out_size()):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)

    def _from_spin(self):
        k = self.view.k
        s = QRectF(self.view.sel)
        s.setWidth(self.w.value() / k)
        s.setHeight(self.h.value() / k)
        self.view.sel = s.intersected(QRectF(0, 0, self.view.image.width(), self.view.image.height()))
        self.view.update()
        self._to_spin()

    def reset_ratio(self):
        self.ratio_box.blockSignals(True)
        self.ratio_box.setCurrentIndex(0)
        self.ratio_box.blockSignals(False)
        self.view.ratio = None


class CropWindow(CropInspector, ToolWindow):
    tool_name = "Crop"

    def __init__(self, path, notify):
        super().__init__("Crop Image", path, notify, size=(1000, 680), toolbar=True)
        pv = open_preview(path, 1400)
        with Image.open(path) as im:
            fw, fh = im.size
            o = im.getexif().get(0x0112, 1)
        if o in (5, 6, 7, 8):
            fw, fh = fh, fw
        self.full_size = (fw, fh)
        self.rot = 0
        self.base = pv
        self.view = CropView(qimage_from_pil(pv), fw / pv.width)
        self.set_canvas(self.view)
        self.toolbar.add(("rotate.left", "Rotate Left", "⟲", lambda: self._rotate(-90)),
                         ("rotate.right", "Rotate Right", "⟳", lambda: self._rotate(90)))
        self.toolbar.add(("arrow.counterclockwise", "Reset", "↺", self.reset))
        self.crop_inspector()
        self.finish("Apply", self.apply)

    def _rotate(self, d):
        self.rot = (self.rot + d) % 360
        img = self.base.rotate(-self.rot, expand=True)
        k = self.view.k
        self.view.set_image(qimage_from_pil(img))
        self.view.k = k
        self.view.reset_sel()

    def reset(self):
        self.reset_ratio()
        if self.rot:
            self.rot = 0
            self.view.set_image(qimage_from_pil(self.base))
        self.view.reset_sel()

    def apply(self):
        rot, path = self.rot, self.path
        full = (self.full_size[1], self.full_size[0]) if rot % 180 else self.full_size
        x0, y0, w, h = crop_box(self.view.sel, self.view.k, full, self.view.ratio)
        box = (x0, y0, x0 + w, y0 + h)

        def work():
            im = open_image(path)
            if rot:
                im = im.rotate(-rot, expand=True)
            return [imaging.save_like(im.crop(box), path, "Cropped")]
        extra = f"rotated {rot}°" if rot else ""
        self.run(work, "Cropping…", extra)


# ================================================================ ADD BG
PRESETS = [
    ("Sunset", ["#ffb36b", "#ff5f6d", "#a24bff"]),
    ("Prism", ["#ff6b9d", "#ffb36b", "#ffe66b", "#7dffb0", "#6bd5ff", "#8a7bff"]),
    ("Peach", ["#ffd6a5", "#ff9a8b"]),
    ("Aurora", ["#00d2ff", "#7a5cff", "#ff5fd2"]),
    ("Mint", ["#a8ffce", "#3ad6c6"]),
    ("Ocean", ["#43cbff", "#2058d6"]),
    ("Dusk", ["#2b1055", "#d53a9d"]),
    ("Lemon", ["#fff59d", "#ffb74d"]),
    ("Night", ["#0f2027", "#2c5364"]),
    ("Rose", ["#ffdde1", "#ee9ca7"]),
]
SOLIDS = ["#ffffff", "#f2f2f7", "#d1d1d6", "#8e8e93", "#1c1c1e", "#000000", "#ff3b30",
          "#ff9500", "#ffcc00", "#34c759", "#00c7be", "#007aff", "#5856d6", "#af52de"]


class AddBgWindow(ToolWindow):
    tool_name = "Add BG"
    ASPECTS = ["Auto", "1:1", "4:3", "16:9", "9:16", "4:5"]

    def __init__(self, path, notify):
        super().__init__("Add Background", path, notify, size=(1040, 720), toolbar=True)
        self.small = open_preview(path, 760)
        with Image.open(path) as im:
            self.full_w = max(im.size) if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else im.width
            self.full_size = im.size
        self.colors = PRESETS[0][1]

        self.preview = ImageView(QImage())
        self.set_canvas(self.preview)
        self.copy_btn, = self.toolbar.add(("doc.on.clipboard", "Copy", "⧉", self.copy))
        self.copy_btn.setToolTip("Copy the result to the clipboard")
        self.toolbar.add(("arrow.counterclockwise", "Reset", "↺", self.reset))

        g = self.inspector.group("Fill")
        grid = form_grid()
        self.well = controls.ColorWell(PRESETS + [(c, [c]) for c in SOLIDS], self.colors, columns=8)
        self.well.picked.connect(self._pick)
        grid.addWidget(controls.form_label("Colour"), 0, 0)
        grid.addWidget(self.well, 0, 1, 1, 2, Qt.AlignLeft)
        self.ang = slider(0, 360, 135)
        slider_row(grid, 1, "Angle", self.ang, lambda v: f"{v}°", "°")
        g.addLayout(grid)

        g = self.inspector.group("Layout")
        grid = form_grid()
        self.pad = slider(0, 40, 12)
        self.rad = slider(0, 120, 24)
        self.sha = slider(0, 100, 50)
        slider_row(grid, 0, "Padding", self.pad, lambda v: f"{v}%", "%")
        slider_row(grid, 1, "Corners", self.rad, lambda v: f"{v} px", " px")
        slider_row(grid, 2, "Shadow", self.sha, lambda v: f"{v}%", "%")
        g.addLayout(grid)

        g = self.inspector.group("Size")
        grid = form_grid()
        self.asp = QComboBox()
        self.asp.setAccessibleName("Aspect ratio")
        self.asp.addItems(self.ASPECTS)
        grid.addWidget(controls.form_label("Aspect"), 0, 0)
        grid.addWidget(self.asp, 0, 1)
        g.addLayout(grid)
        self.out_label = QLabel()
        self.out_label.setObjectName("dim")
        g.addWidget(self.out_label)
        self.finish("Save", self.save)

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        for s in (self.pad, self.rad, self.sha, self.ang):
            s.valueChanged.connect(lambda _: self._timer.start())
        self.asp.currentIndexChanged.connect(lambda _: self._timer.start())
        self.render()

    def _pick(self, cols):
        self.colors = cols
        self.well.set_colors(cols)
        self.render()

    def _params(self, scale: float) -> imaging.Background:
        return imaging.Background(colors=self.colors, angle=self.ang.value(),
                                  padding=self.pad.value() / 100,
                                  radius=int(self.rad.value() * scale),
                                  shadow=self.sha.value() / 100,
                                  aspect=self.ASPECTS[self.asp.currentIndex()])

    def _full_scale(self) -> float:
        return max(1.0, max(self.full_size) / 1000)

    def render(self):
        k = max(self.small.size) / max(self.full_size)
        out = imaging.add_background(self.small, self._params(k * self._full_scale()))
        self.preview.set_image(qimage_from_pil(out))
        fw = round(out.width / k)
        fh = round(out.height / k)
        self.out_label.setText(f"Output  {dims_text(fw, fh)} px  ·  PNG")

    def reset(self):
        self.pad.setValue(12); self.rad.setValue(24); self.sha.setValue(50); self.ang.setValue(135)
        self.asp.setCurrentIndex(0)
        self._pick(PRESETS[0][1])

    def _compose_full(self) -> Image.Image:
        return imaging.add_background(open_image(self.path), self._params(self._full_scale()))

    def copy(self):
        QGuiApplication.clipboard().setImage(qimage_from_pil(self._compose_full()))
        self.copy_btn.set_label("Copied")
        QTimer.singleShot(1400, lambda: self.copy_btn.set_label("Copy"))

    def save(self):
        params, path = self._params(self._full_scale()), self.path

        def work():
            out_im = imaging.add_background(open_image(path), params)
            out = sibling(path, "With Background", ".png")
            from .convert import save_image
            save_image(out_im, out, "PNG")
            return [out]
        self.run(work, "Adding background…")


# ================================================================ ANNOTATE
class Shape:
    def __init__(self, kind, color, width, pts=None, text=""):
        self.kind, self.color, self.width = kind, QColor(color), width
        self.pts: list[QPointF] = pts or []
        self.text = text


def draw_shape(p: QPainter, s: Shape, map_pt, scale: float):
    pts = [map_pt(q) for q in s.pts]
    w = max(1.0, s.width * scale)
    col = QColor(s.color)
    if s.kind == "Highlight":
        col.setAlpha(110)
        w *= 4
    pen = QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if s.kind in ("Pen", "Highlight") and len(pts) > 1:
        path = QPainterPath(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        p.drawPath(path)
    elif s.kind == "Arrow" and len(pts) == 2:
        a, b = pts
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        head = max(12.0, w * 4.2)
        base = QPointF(b.x() - head * 0.8 * math.cos(ang), b.y() - head * 0.8 * math.sin(ang))
        p.drawLine(a, base)
        left = QPointF(b.x() - head * math.cos(ang - 0.42), b.y() - head * math.sin(ang - 0.42))
        right = QPointF(b.x() - head * math.cos(ang + 0.42), b.y() - head * math.sin(ang + 0.42))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([b, left, right]))
    elif s.kind == "Rect" and len(pts) == 2:
        p.drawRoundedRect(QRectF(pts[0], pts[1]).normalized(), w, w)
    elif s.kind == "Ellipse" and len(pts) == 2:
        p.drawEllipse(QRectF(pts[0], pts[1]).normalized())
    elif s.kind == "Text" and pts:
        f = design.font("body", 700, max(8, int(s.width * 5 * scale)))
        p.setFont(f)
        # outline for legibility on any background
        outline = QPainterPath()
        outline.addText(pts[0], f, s.text)
        lum = col.red() * 0.3 + col.green() * 0.59 + col.blue() * 0.11
        p.setPen(QPen(QColor(0, 0, 0, 160) if lum > 128 else QColor(255, 255, 255, 190), max(2.0, w * 0.6)))
        p.drawPath(outline)
        p.fillPath(outline, col)
    elif s.kind == "Step" and pts:
        r = max(10.0, s.width * 3.2 * scale)
        c = pts[0]
        p.setPen(QPen(QColor(255, 255, 255), max(1.5, r * 0.12)))
        p.setBrush(col)
        p.drawEllipse(c, r, r)
        p.setFont(design.font("body", 700, int(r * 1.15)))
        lum = col.red() * 0.3 + col.green() * 0.59 + col.blue() * 0.11
        p.setPen(QColor(20, 20, 20) if lum > 170 else QColor(255, 255, 255))
        p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, s.text)


class AnnotateView(ImageView):
    def __init__(self, image: QImage, owner: "AnnotateWindow"):
        super().__init__(image)
        self.owner = owner
        self.shapes: list[Shape] = []
        self.redo_stack: list[Shape] = []
        self.cur: Shape | None = None
        self.editor: QLineEdit | None = None
        self.setCursor(Qt.CrossCursor)

    def next_step(self) -> int:
        return sum(1 for s in self.shapes if s.kind == "Step") + 1

    def _commit_text(self):
        ed = self.editor
        if ed is None:
            return
        self.editor = None
        text = ed.text().strip()
        if text:
            self.add(Shape("Text", ed.property("color"), ed.property("w"), [ed.property("pt")], text))
        ed.deleteLater()

    def add(self, s: Shape):
        self.shapes.append(s)
        self.redo_stack.clear()
        self.update()

    def mousePressEvent(self, e):
        self._commit_text()
        if e.button() != Qt.LeftButton or not self.target_rect().contains(e.position()):
            return
        o = self.owner
        p = self.to_image(e.position())
        width = o.stroke.value() / self.scale()  # keep on-screen thickness consistent
        if o.tool == "Text":
            ed = QLineEdit(self)
            ed.setPlaceholderText("Type, then Enter")
            ed.setProperty("pt", p)
            ed.setProperty("color", o.color)
            ed.setProperty("w", width)
            ed.move(int(e.position().x()), int(e.position().y()) - 16)
            ed.resize(220, 30)
            ed.show()
            ed.setFocus()
            ed.returnPressed.connect(self._commit_text)
            self.editor = ed
            return
        if o.tool == "Step":
            self.add(Shape("Step", o.color, width, [p], str(self.next_step())))
            return
        self.cur = Shape(o.tool, o.color, width, [p, p] if o.tool not in ("Pen", "Highlight") else [p])

    def mouseMoveEvent(self, e):
        if not self.cur:
            return
        p = self.clamp_image(self.to_image(e.position()))
        if self.cur.kind in ("Pen", "Highlight"):
            self.cur.pts.append(p)
        else:
            self.cur.pts[1] = p
        self.update()

    def mouseReleaseEvent(self, e):
        if self.cur:
            self.add(self.cur)
            self.cur = None

    def paint_overlay(self, p, t):
        s = self.scale()
        for sh in self.shapes + ([self.cur] if self.cur else []):
            draw_shape(p, sh, self.to_widget, s)


class AnnotateWindow(ToolWindow):
    tool_name = "Annotate"
    TOOLS = ["Arrow", "Rect", "Ellipse", "Pen", "Highlight", "Text", "Step"]
    COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#af52de", "#ffffff", "#000000"]

    def __init__(self, path, notify):
        super().__init__("Annotate", path, notify, size=(1100, 740), toolbar=True)
        pv = open_preview(path, 1600)
        with Image.open(path) as im:
            full = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.k = full[0] / pv.width
        self.tool, self.color = "Arrow", self.COLORS[0]
        self.view = AnnotateView(qimage_from_pil(pv), self)
        self.set_canvas(self.view)

        specs = [("arrow.up.right", "Arrow", "➜"), ("rectangle", "Box", "▢"), ("circle", "Circle", "◯"),
                 ("pencil.tip", "Pen", "✎"), ("highlighter", "Marker", "▬"), ("textformat", "Text", "T"),
                 ("1.circle", "Step", "①")]
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for i, b in enumerate(self.toolbar.add(*specs, checkable=True)):
            b.setChecked(i == 0)
            grp.addButton(b, i)
        grp.idClicked.connect(lambda i: setattr(self, "tool", self.TOOLS[i]))
        self.toolbar.add(("arrow.uturn.backward", "Undo", "↶", self.undo),
                         ("arrow.uturn.forward", "Redo", "↷", self.redo))
        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)

        g = self.inspector.group("Style")
        grid = form_grid()
        names = ["Red", "Orange", "Yellow", "Green", "Blue", "Purple", "White", "Black"]
        self.well = controls.ColorWell([(n, [c]) for n, c in zip(names, self.COLORS)], [self.color], columns=8)
        self.well.picked.connect(lambda cols: setattr(self, "color", cols[0]))
        grid.addWidget(controls.form_label("Colour"), 0, 0)
        grid.addWidget(self.well, 0, 1, 1, 2, Qt.AlignLeft)
        self.stroke = slider(1, 20, 4)
        slider_row(grid, 1, "Size", self.stroke, str, " pt")
        g.addLayout(grid)
        self.finish("Save", self.save)

    def undo(self):
        if self.view.shapes:
            self.view.redo_stack.append(self.view.shapes.pop())
            self.view.update()

    def redo(self):
        if self.view.redo_stack:
            self.view.shapes.append(self.view.redo_stack.pop())
            self.view.update()

    def save(self):
        self.view._commit_text()
        shapes = list(self.view.shapes)
        if not shapes:
            self.close()
            return
        k, path = self.k, self.path
        counts: dict[str, int] = {}
        for s in shapes:
            counts[s.kind] = counts.get(s.kind, 0) + 1
        names = {"Arrow": "arrow", "Rect": "box", "Ellipse": "circle", "Pen": "stroke", "Highlight": "highlight",
                 "Text": "label", "Step": "step"}
        extra = ", ".join(f"{n} {names[t]}{'s' if n > 1 else ''}" for t, n in counts.items())

        def work():
            img = qimage_from_pil(open_image(path).convert("RGBA"))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            for sh in shapes:
                draw_shape(p, sh, lambda q: QPointF(q.x() * k, q.y() * k), k)
            p.end()
            return [imaging.save_like(pil_from_qimage(img), path, "Annotated")]
        self.run(work, "Saving annotations…", extra)


# ================================================================ REDACT
class RedactView(ImageView):
    def __init__(self, image: QImage, owner: "RedactWindow"):
        super().__init__(image)
        self.owner = owner
        self.rects: list[QRectF] = []
        self.cur: QRectF | None = None
        self._a = QPointF()
        self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.target_rect().contains(e.position()):
            self._a = self.clamp_image(self.to_image(e.position()))
            self.cur = QRectF(self._a, self._a)

    def mouseMoveEvent(self, e):
        if self.cur is not None:
            self.cur = QRectF(self._a, self.clamp_image(self.to_image(e.position()))).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.cur is not None and self.cur.width() > 2 and self.cur.height() > 2:
            self.rects.append(self.cur)
            self.owner.refresh()
        self.cur = None
        self.update()

    def paint_overlay(self, p, t):
        if self.cur is not None:
            p.setPen(QPen(design.color("red"), 1.5, Qt.DashLine))
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRect(self.rect_to_widget(self.cur))


class RedactWindow(ToolWindow):
    tool_name = "Redact"
    MODES = ["Black box", "Pixelate", "Blur"]

    def __init__(self, path, notify):
        super().__init__("Redact", path, notify, size=(1000, 680), toolbar=True)
        self.base = open_preview(path, 1400)
        with Image.open(path) as im:
            full = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.k = full[0] / self.base.width
        self.view = RedactView(qimage_from_pil(self.base), self)
        self.set_canvas(self.view)
        self.toolbar.add(("arrow.uturn.backward", "Undo", "↶", self.undo))
        QShortcut(QKeySequence.Undo, self, activated=self.undo)

        g = self.inspector.group("Style")
        row, self.mode = toggle_row(self.MODES)
        self.mode.idClicked.connect(lambda _: self.refresh())
        g.addLayout(row)
        self.hint = QLabel("Drag over anything you want hidden. It's burned into the pixels, so it can't be "
                           "undone in the copy.")
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        g.addWidget(self.hint)
        self.finish("Save", self.save)

    def _mode(self):
        return ["black", "pixelate", "blur"][self.mode.checkedId()]

    def _boxes(self, k: float):
        return [tuple(round(v * k) for v in (r.left(), r.top(), r.right(), r.bottom())) for r in self.view.rects]

    def refresh(self):
        im = imaging.redact(self.base, self._boxes(1.0), self._mode())
        self.view.set_image(qimage_from_pil(im))
        n = len(self.view.rects)
        self.set_subtitle(f"{self.path.name}  ·  {plural(n, 'area')} hidden" if n else self.path.name)

    def undo(self):
        if self.view.rects:
            self.view.rects.pop()
            self.refresh()

    def save(self):
        boxes, mode, path = self._boxes(self.k), self._mode(), self.path
        if not boxes:
            self.close()
            return
        label = {"black": "black boxes", "pixelate": "pixelated", "blur": "blurred"}[mode]

        def work():
            return [imaging.save_like(imaging.redact(open_image(path), boxes, mode), path, "Redacted")]
        self.run(work, "Redacting…", f"{plural(len(boxes), 'area')} {label}")


# ================================================================ EDIT
class EditWindow(ToolWindow):
    tool_name = "Edit"

    def __init__(self, path, notify):
        super().__init__("Edit", path, notify, size=(1040, 720), toolbar=True)
        self.small = open_preview(path, 900)
        with Image.open(path) as im:
            self.full_size = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.adj = imaging.Adjust()
        self.comparing = False

        self.preview = ImageView(QImage())
        self.set_canvas(self.preview)
        auto, = self.toolbar.add(("wand.and.stars", "Auto", "✦", self.auto))
        auto.setToolTip("Automatic light and colour")
        self.toolbar.add(("rotate.left", "Rotate Left", "⟲", lambda: self._rot(-90)),
                         ("rotate.right", "Rotate Right", "⟳", lambda: self._rot(90)),
                         ("arrow.left.and.right.righttriangle.left.righttriangle.right", "Flip", "⇋",
                          lambda: self._flip("h")),
                         ("arrow.up.and.down.righttriangle.up.righttriangle.down", "Flip Vertical", "⇵",
                          lambda: self._flip("v")))
        # a toggle, not hold-to-compare: native toolbar items only report clicks
        self.cmp_btn, = self.toolbar.add(("square.split.2x1", "Compare", "◐"), checkable=True)
        self.cmp_btn.setToolTip("Show the original")
        self.cmp_btn.toggled.connect(self._compare)
        self.toolbar.add(("arrow.counterclockwise", "Reset", "↺", self.reset))

        g = self.inspector.group("Light and Colour")
        grid = form_grid()
        self.sl = {
            "brightness": slider(-100, 100, 0), "contrast": slider(-100, 100, 0),
            "saturation": slider(-100, 100, 0), "warmth": slider(-100, 100, 0),
            "sharpness": slider(-100, 100, 0),
        }
        for i, (k, s) in enumerate(self.sl.items()):
            slider_row(grid, i, k.capitalize(), s, lambda v: f"{v:+d}", "")
            s.valueChanged.connect(lambda _: self._timer.start())
        g.addLayout(grid)
        self.gray = QCheckBox("Black && White")
        self.gray.toggled.connect(lambda _: self._timer.start())
        g.addWidget(self.gray)

        g = self.inspector.group("Size")
        grid = form_grid()
        self.size_sl = slider(5, 100, 100)
        slider_row(grid, 0, "Scale", self.size_sl, lambda v: f"{v}%", "%")
        self.size_sl.valueChanged.connect(self._size_label)
        self.dim_label = QLabel()
        self.dim_label.setObjectName("dim")
        grid.addWidget(self.dim_label, 1, 1, 1, 2)
        g.addLayout(grid)
        self.finish("Save", self.save)

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        self._size_label()
        self.render()

    def _compare(self, on: bool):
        self.comparing = on
        self.render()

    def auto(self):
        a = imaging.auto_enhance(self.small)
        vals = {"brightness": a.brightness - 1, "contrast": a.contrast - 1, "saturation": a.saturation - 1,
                "sharpness": (a.sharpness - 1) / 2}
        for k, v in vals.items():
            self.sl[k].setValue(int(round(v * 100)))

    def _size_label(self):
        p = self.size_sl.value() / 100
        w, h = self.full_size
        if self.adj.rotate % 180:
            w, h = h, w
        self.dim_label.setText(f"{round(w * p)} × {round(h * p)} px")

    def _rot(self, d):
        self.adj.rotate = (self.adj.rotate + d) % 360
        self._size_label()
        self.render()

    def _flip(self, axis):
        if axis == "h":
            self.adj.flip_h = not self.adj.flip_h
        else:
            self.adj.flip_v = not self.adj.flip_v
        self.render()

    def _collect(self, resize: bool) -> imaging.Adjust:
        f = lambda k: 1 + self.sl[k].value() / 100
        a = self.adj
        a.brightness, a.contrast, a.saturation = f("brightness"), f("contrast"), f("saturation")
        a.sharpness = 1 + self.sl["sharpness"].value() / 50
        a.warmth = self.sl["warmth"].value() / 100
        a.grayscale = self.gray.isChecked()
        a.resize_pct = self.size_sl.value() if resize else 100
        return a

    def render(self):
        out = self.small if self.comparing else imaging.apply_adjust(self.small, self._collect(False))
        self.preview.set_image(qimage_from_pil(out))

    def reset(self):
        for s in self.sl.values():
            s.setValue(0)
        self.size_sl.setValue(100)
        self.gray.setChecked(False)
        self.cmp_btn.setChecked(False)
        self.adj = imaging.Adjust()
        self._size_label()
        self.render()

    def _summary(self) -> str:
        bits = [f"{k} {s.value():+d}" for k, s in self.sl.items() if s.value()]
        if self.adj.rotate:
            bits.append(f"rotated {self.adj.rotate}°")
        if self.adj.flip_h or self.adj.flip_v:
            bits.append("flipped")
        if self.gray.isChecked():
            bits.append("B&W")
        return ", ".join(bits[:4])

    def save(self):
        adj = imaging.Adjust(**vars(self._collect(True)))
        path = self.path

        def work():
            return [imaging.save_like(imaging.apply_adjust(open_image(path), adj), path, "Edited")]
        self.run(work, "Applying edits…", self._summary())


# ================================================================ METADATA
class MetadataWindow(ToolWindow):
    tool_name = "Metadata"

    def __init__(self, path, notify, is_pdf: bool = False):
        super().__init__("Metadata", path, notify, size=(640, 330))
        self.is_pdf = is_pdf
        info = imaging.read_pdf_metadata(path) if is_pdf else imaging.read_image_metadata(path)
        summary = {} if is_pdf else imaging.metadata_summary(path)
        self.n_fields, self.has_gps = (len(info), False) if is_pdf else imaging.count_metadata(path)

        g = self.inspector.group("Summary", collapsible=False)
        cl = form_grid()
        cl.setHorizontalSpacing(design.SPACING["m"])
        rows = summary or {k: v for k, v in info.items() if k in ("title", "author", "creator", "producer", "Pages")}
        if not rows:
            rows = {"": "No personal metadata found."}
        for i, (k, v) in enumerate(rows.items()):
            kl = QLabel(k)
            kl.setObjectName("dim")
            kl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            cl.addWidget(kl, i, 0)
            vl = QLabel(v)
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cl.addWidget(vl, i, 1)
            if k == "Location":
                vl.setObjectName("warn")
                mb = QPushButton("Open Map")
                lat, lon = v.split(", ")
                mb.clicked.connect(lambda _=False, a=lat, b=lon: QDesktopServices.openUrl(
                    QUrl(f"https://www.openstreetmap.org/?mlat={a}&mlon={b}#map=15/{a}/{b}")))
                cl.addWidget(mb, i, 2)
        cl.setColumnStretch(0, 0)
        cl.setColumnStretch(1, 1)
        g.addLayout(cl)
        if self.has_gps:
            row = QHBoxLayout()
            row.setSpacing(design.SPACING["xs"])
            icon = QLabel()
            pm = design.symbol("exclamationmark.triangle.fill", 12, design.color("orange"), fallback="⚠")
            if pm is not None:
                icon.setPixmap(pm)
            row.addWidget(icon)
            warn = QLabel("This photo reveals where it was taken.")
            warn.setObjectName("warn")
            row.addWidget(warn, 1)
            g.addLayout(row)

        g = self.inspector.group(f"All Fields ({self.n_fields})", expanded=False)
        self.toggle = self.inspector.headers[-1]
        t = QTableWidget(len(info), 2)
        t.setHorizontalHeaderLabels(["Field", "Value"])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setMinimumHeight(220)
        for r, (k, v) in enumerate(info.items()):
            t.setItem(r, 0, QTableWidgetItem(k))
            t.setItem(r, 1, QTableWidgetItem(v))
        g.addWidget(t)
        self.toggle.toggled.connect(lambda on: self.resize(self.width(), 640 if on else 330))

        extra = []
        if self.has_gps:
            loc = QPushButton("Remove Location Only")
            loc.clicked.connect(self.strip_location)
            extra.append(loc)
        self.finish("Remove All Metadata", self.strip, None, extra)

    def strip(self):
        path, is_pdf = self.path, self.is_pdf
        detail = f"removed {plural(self.n_fields, 'field')}" + (" incl. GPS location" if self.has_gps else "")

        def work():
            return [imaging.strip_pdf_metadata(path) if is_pdf else imaging.strip_image_metadata(path)]
        self.run(work, "Removing metadata…", detail)

    def strip_location(self):
        path = self.path
        self.run(lambda: [imaging.strip_location(path)], "Removing location…", "GPS removed, camera info kept")


# ================================================================ COMPRESS
class CompressWindow(ToolWindow):
    tool_name = "Compress"
    SIDES = [("Original size", None), ("Max 3840 px", 3840), ("Max 2560 px", 2560),
             ("Max 1920 px", 1920), ("Max 1280 px", 1280), ("Max 800 px", 800)]
    TARGETS = [("2 MB", 2_000_000), ("1 MB", 1_000_000), ("500 KB", 500_000), ("200 KB", 200_000), ("100 KB", 100_000)]

    def __init__(self, paths, notify):
        super().__init__("Compress", paths, notify, size=(640, 600))
        self.orig_total = sum(file_size(p) for p in self.paths)
        self.pv = open_preview(self.path, 1600)
        with Image.open(self.path) as im:
            self.full_px = im.width * im.height
            self.full_long = max(im.size)

        g = self.inspector.group("Compression", collapsible=False)
        row, self.mode = toggle_row(["Quality", "Target Size", "Percentage"])
        g.addLayout(row)

        self.q_box = QWidget()
        grid = form_grid()
        self.q_box.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        self.q = slider(10, 95, 72)
        slider_row(grid, 0, "Quality", self.q, lambda v: f"{v}", "")
        g.addWidget(self.q_box)

        self.t_box = QWidget()
        tl = QHBoxLayout(self.t_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(controls.form_label("Make each file under"))
        trow, self.tgrp = toggle_row([lbl for lbl, _ in self.TARGETS], 2)
        tl.addLayout(trow, 1)
        self.t_box.setVisible(False)
        g.addWidget(self.t_box)

        self.p_box = QWidget()
        pgrid = form_grid()
        pgrid.setContentsMargins(0, 0, 0, 0)
        self.p_box.setLayout(pgrid)
        self.pct = percent_row(pgrid, 0)
        self.p_box.setVisible(False)
        g.addWidget(self.p_box)

        g = self.inspector.group("Output", collapsible=False)
        g2 = form_grid()
        g2.addWidget(controls.form_label("Size"), 0, 0)
        self.side = QComboBox()
        self.side.setAccessibleName("Size")
        for name, _ in self.SIDES:
            self.side.addItem(name)
        g2.addWidget(self.side, 0, 1, Qt.AlignLeft)
        self.webp = QCheckBox("Convert to WebP (usually the smallest)")
        g2.addWidget(self.webp, 1, 1)
        g.addLayout(g2)

        g = self.inspector.group("Preview at 100%", collapsible=False)
        pr = QHBoxLayout()
        pr.setSpacing(design.SPACING["m"])
        self.before = QLabel()
        self.after = QLabel()
        for lab, cap in ((self.before, "Original"), (self.after, "Compressed")):
            col = QVBoxLayout()
            col.setSpacing(design.SPACING["xs"])
            lab.setFixedSize(270, 170)
            lab.setAlignment(Qt.AlignCenter)
            lab.setObjectName("preview")
            lab.setAccessibleName(f"{cap} preview")
            c = QLabel(cap)
            c.setObjectName("dim")
            col.addWidget(lab)
            col.addWidget(c, 0, Qt.AlignHCenter)
            pr.addLayout(col)
        pr.addStretch(1)
        g.addLayout(pr)

        self.est = QLabel()
        self.est.setObjectName("big")
        self.saving = QLabel()
        self.saving.setObjectName("good")
        g.addWidget(self.est)
        g.addWidget(self.saving)
        label = "Compress" if len(self.paths) == 1 else f"Compress {len(self.paths)} Files"
        self.finish(label, self.save)

        self._timer = QTimer(self, singleShot=True, interval=60, timeout=self.estimate)
        self.q.valueChanged.connect(lambda _: self._timer.start())
        self.side.currentIndexChanged.connect(lambda _: self._timer.start())
        self.webp.toggled.connect(lambda _: self._timer.start())
        self.tgrp.idClicked.connect(lambda _: self._timer.start())
        self.pct.valueChanged.connect(lambda _: self._timer.start())
        self.mode.idClicked.connect(self._mode)
        self._set_before()
        self.estimate()

    def _mode(self, i):
        self.q_box.setVisible(i == 0)
        self.t_box.setVisible(i == 1)
        self.p_box.setVisible(i == 2)
        self._timer.start()

    def _detail_box(self, im: Image.Image) -> tuple[int, int, int, int]:
        """The 270×170 window with the most detail (where compression artefacts show)."""
        from PIL import ImageFilter, ImageStat
        w, h = 270, 170
        if im.width <= w or im.height <= h:
            return (0, 0, min(w, im.width), min(h, im.height))
        edges = im.convert("L").filter(ImageFilter.FIND_EDGES)
        best, box = -1.0, (0, 0, w, h)
        for gx in range(5):
            for gy in range(5):
                x = int((im.width - w) * gx / 4)
                y = int((im.height - h) * gy / 4)
                v = ImageStat.Stat(edges.crop((x, y, x + w, y + h))).mean[0]
                if v > best:
                    best, box = v, (x, y, x + w, y + h)
        return box

    def _crop_100(self, im: Image.Image) -> QPixmap:
        if not hasattr(self, "_box"):
            self._box = self._detail_box(self.pv)
        return QPixmap.fromImage(qimage_from_pil(im.crop(self._box)))

    def _set_before(self):
        self.before.setPixmap(self._crop_100(self.pv))

    def _settings(self):
        side = self.SIDES[self.side.currentIndex()][1]
        target = imaging.lossy_target(self.path, self.webp.isChecked())
        return side, target

    def estimate(self):
        side, target = self._settings()
        im = self.pv
        # scale factor from preview pixels to output pixels
        out_long = min(self.full_long, side) if side else self.full_long
        px_ratio = (out_long / max(im.size)) ** 2
        if self.mode.checkedId() in (1, 2):
            limit = self._limit(self.path)
            q = self._quality_for(im, target, limit, px_ratio)
            note = f"quality ≈ {q}"
        else:
            q = self.q.value()
            note = ""
        n = int(imaging.encode_size(im, target, q) * px_ratio)
        buf = io.BytesIO()
        if target == "PNG":
            im.convert("RGBA").quantize(colors=max(16, min(256, int(q * 2.56)))).save(buf, "PNG")
        else:
            from .convert import save_image
            save_image(im, buf, target, quality=q)
        buf.seek(0)
        try:
            self.after.setPixmap(self._crop_100(Image.open(buf).convert("RGB")))
        except Exception:
            pass
        orig1 = file_size(self.path)
        if len(self.paths) > 1:
            total = int(n * self.orig_total / max(1, orig1))
            self.est.setText(f"{human_size(self.orig_total)}  →  ≈ {human_size(total)}")
            n_cmp, o_cmp = total, self.orig_total
        else:
            self.est.setText(f"{human_size(orig1)}  →  ≈ {human_size(n)}")
            n_cmp, o_cmp = n, orig1
        pct = (1 - n_cmp / max(1, o_cmp)) * 100
        fmt = target if target != "JPG" else "JPG"
        if pct >= 1:
            self.saving.setObjectName("good")
            self.saving.setText(f"≈ {pct:.0f}% smaller · {fmt}" + (f" · {note}" if note else ""))
        else:
            self.saving.setObjectName("warn")
            self.saving.setText("Already well compressed. Try a smaller size or WebP.")
        self.saving.style().unpolish(self.saving)
        self.saving.style().polish(self.saving)

    def _limit(self, path) -> int:
        """Byte budget for one file: the picked target size, or a share of its own size."""
        if self.mode.checkedId() == 2:
            return int(file_size(path) * self.pct.value() / 100)
        return self.TARGETS[self.tgrp.checkedId()][1]

    @staticmethod
    def _quality_for(im, target, limit, px_ratio) -> int:
        lo, hi, best = 30, 92, 30
        while lo <= hi:
            q = (lo + hi) // 2
            if imaging.encode_size(im, target, q) * px_ratio <= limit:
                best, lo = q, q + 1
            else:
                hi = q - 1
        return best

    def save(self):
        side, target = self._settings()
        paths = list(self.paths)
        if self.mode.checkedId() in (1, 2):
            limits = {p: self._limit(p) for p in paths}
            if self.mode.checkedId() == 2:
                label = f"aimed for {self.pct.value()}%"
            else:
                label = f"target {self.TARGETS[self.tgrp.checkedId()][0]}"

            def work():
                outs = []
                for p in paths:
                    im = open_image(p)
                    if side and max(im.size) > side:
                        im.thumbnail((side, side), Image.LANCZOS)
                    q, s2 = imaging.plan_target_size(im, target, limits[p])
                    outs.append(imaging.compress_to(p, q, min(x for x in (side, s2) if x) if (side or s2) else None, target))
                return outs
            self.run(work, "Compressing…", label)
        else:
            q = self.q.value()
            self.run(lambda: [imaging.compress_to(p, q, side, target) for p in paths], "Compressing…", f"quality {q}")


# ================================================================ RESIZE
class ResizeWindow(ToolWindow):
    tool_name = "Resize"
    PRESETS = [("25%", "pct", 25), ("50%", "pct", 50), ("75%", "pct", 75), ("3840", "long", 3840),
               ("2560", "long", 2560), ("1920", "long", 1920), ("1280", "long", 1280), ("800", "long", 800)]

    def __init__(self, paths, notify):
        super().__init__("Resize", paths, notify, size=(600, 400))
        with Image.open(self.path) as im:
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
        self.orig = (w, h)
        g = self.inspector.group("Presets", collapsible=False)
        row, self.grp = toggle_row([p[0] for p in self.PRESETS], 1)
        self.grp.idClicked.connect(self._preset)
        g.addLayout(row)
        hint = QLabel("Percent, or the longest side in pixels")
        hint.setObjectName("dim")
        g.addWidget(hint)
        g = self.inspector.group("Exact Size", collapsible=False)
        er = QHBoxLayout()
        er.setSpacing(design.SPACING["m"])
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, 30000)
            s.setFixedWidth(100)
        self.lock = QCheckBox("Keep proportions")
        self.lock.setChecked(True)
        er.addLayout(size_fields(self.w, self.h))
        er.addWidget(self.lock, 0, Qt.AlignTop)
        er.addStretch(1)
        g.addLayout(er)
        self.out = QLabel()
        self.out.setObjectName("big")
        g.addWidget(self.out)
        label = "Resize" if len(self.paths) == 1 else f"Resize {len(self.paths)} Files"
        self.finish(label, self.save)
        self.w.valueChanged.connect(lambda v: self._typed("w", v))
        self.h.valueChanged.connect(lambda v: self._typed("h", v))
        self._preset(1)

    def _set(self, w, h):
        for box, v in ((self.w, w), (self.h, h)):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)
        self.out.setText(f"{dims_text(*self.orig)}  →  {dims_text(w, h)}")

    def _preset(self, i):
        _, mode, val = self.PRESETS[i]
        self.mode = (mode, val)
        self._set(*imaging.resize_dims(self.orig, mode, val))

    def _typed(self, which, v):
        b = self.grp.checkedButton()
        if b:
            self.grp.setExclusive(False)
            b.setChecked(False)
            self.grp.setExclusive(True)
        if self.lock.isChecked():
            w, h = imaging.resize_dims(self.orig, which, v)
        else:
            w, h = self.w.value(), self.h.value()
        self.mode = (which, v) if self.lock.isChecked() else ("exact", (w, h))
        self._set(w, h)

    def save(self):
        mode, val = self.mode
        paths = list(self.paths)

        def dims_for(p):
            with Image.open(p) as im:
                size = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
            if mode == "exact":
                return val
            return imaging.resize_dims(size, mode, val)
        self.run(lambda: [imaging.resize_image(p, dims_for(p)) for p in paths], "Resizing…")


# ================================================================ PROTECT (PDF)
class ProtectWindow(ToolWindow):
    tool_name = "Protect"

    def __init__(self, path, notify):
        super().__init__("Password-Protect PDF", path, notify, size=(520, 300))
        grp = self.inspector.group("Password", collapsible=False)
        g = form_grid()
        self.pw = QLineEdit()
        self.pw2 = QLineEdit()
        for e, name in ((self.pw, "Password"), (self.pw2, "Confirm")):
            e.setEchoMode(QLineEdit.Password)
            e.setAccessibleName(name)
        g.addWidget(controls.form_label("Password"), 0, 0)
        g.addWidget(self.pw, 0, 1)
        g.addWidget(controls.form_label("Confirm"), 1, 0)
        g.addWidget(self.pw2, 1, 1)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: [e.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
                                         for e in (self.pw, self.pw2)])
        g.addWidget(show, 0, 2)
        grp.addLayout(g)
        self.msg = QLabel("AES-256 encryption. Anyone opening the PDF will need this password.")
        self.msg.setObjectName("dim")
        self.msg.setWordWrap(True)
        grp.addWidget(self.msg)
        self.finish("Protect", self.save)
        self.pw.textChanged.connect(self._check)
        self.pw2.textChanged.connect(self._check)
        self._check()
        QTimer.singleShot(0, self.pw.setFocus)

    def _check(self):
        a, b = self.pw.text(), self.pw2.text()
        ok = len(a) >= 4 and a == b
        self.primary_btn.setEnabled(ok)
        if a and len(a) < 4:
            self.msg.setText("Use at least 4 characters.")
        elif b and a != b:
            self.msg.setText("Passwords don't match yet.")
        elif ok:
            strength = "strong" if len(a) >= 12 else "okay" if len(a) >= 8 else "weak"
            self.msg.setText(f"Looks good · {strength} password")
        else:
            self.msg.setText("AES-256 encryption. Anyone opening the PDF will need this password.")

    def save(self):
        pw, path = self.pw.text(), self.path
        self.run(lambda: [imaging.protect_pdf(path, pw)], "Encrypting…")
