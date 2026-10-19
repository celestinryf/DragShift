"""App windows + the image canvas used by all editors.

macOS: a real titled window (traffic lights, native corners and shadow, the window
material from NSVisualEffectView) with the title, and optionally a Pages-style unified
toolbar, in the title bar. Windows/Linux: a frameless liquid-glass window with its own
title bar, painted from the same design tokens."""
from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizeGrip, QVBoxLayout,
                               QWidget)

from . import controls, design, glass

IS_WIN = sys.platform == "win32"


class GlassWindow(QWidget):
    """Top-level window. Put content in `self.body`; toolbar groups in `self.toolbar`
    (only shown when the window is created with toolbar=True)."""

    def __init__(self, title: str, parent=None, size=(760, 640), toolbar: bool = False, subtitle: str = "",
                 show_title: bool = True):
        super().__init__(parent)
        self.native = controls.native()
        self.setWindowTitle(title if self.native else f"{title} · DragShift")
        self.setWindowFlags(Qt.Window if self.native else Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("glassRoot")
        self.setFont(design.font("body"))
        self.resize(*size)
        self._drag: QPoint | None = None
        self._acrylic = False
        self._vibrant = False
        self.radius = design.RADIUS["window"]
        self.liquid = glass.LiquidBacking(self, self.radius)
        self.liquid.changed.connect(self.update)
        self._fade = 0.0
        self.toolbar = controls.Toolbar()
        self.has_toolbar = toolbar
        self._restyle()
        design.theme().changed.connect(self._restyle)

        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        bar = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("title")
        if self.native:
            # title, subtitle and toolbar items are real AppKit chrome (an NSToolbar, like
            # Pages); the Qt content starts under that band (see _fit_band)
            if hasattr(Qt, "WA_ContentsMarginsRespectsSafeArea"):   # we set the margin ourselves
                self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
            self.winId()
            if design._mac().native_chrome(self, toolbar, subtitle, show_title) and toolbar:
                self.toolbar.attach_native(self)
            self._vibrant = design._mac().add_vibrancy(self, "windowBackground")
            self.title_label.hide()
            self.toolbar.hide()
            self._fit_band()
        else:
            outer.setContentsMargins(design.SPACING["l"], design.SPACING["s"], design.SPACING["l"], design.SPACING["m"])
            bar.addWidget(self.title_label)
            self.title_label.setVisible(show_title)
            self._subtitle_label = QLabel(subtitle)
            self._subtitle_label.setObjectName("dim")
            self._subtitle_label.setVisible(bool(subtitle))
            bar.addSpacing(design.SPACING["s"])
            bar.addWidget(self._subtitle_label)
            bar.addStretch(1)
            bar.addWidget(self.toolbar)
            self.toolbar.setVisible(toolbar)
            close = QPushButton("✕")
            close.setObjectName("chrome")
            close.setToolTip("Close (Esc)")
            close.setAccessibleName("Close")
            close.setFlat(True)
            close.setFocusPolicy(Qt.NoFocus)
            close.setFixedSize(26, 22)
            close.clicked.connect(self.close)
            bar.addWidget(close, 0, Qt.AlignTop)
            outer.addLayout(bar)
            outer.addSpacing(design.SPACING["s"])

        self.content = QVBoxLayout()
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(0)
        outer.addLayout(self.content, 1)
        self.body = QVBoxLayout()
        self.body.setSpacing(design.SPACING["m"])
        if self.native:
            m = design.SPACING["xl"]
            self.body.setContentsMargins(m, design.SPACING["s"], m, m)
        self.content.addLayout(self.body, 1)

        if not self.native:   # macOS windows resize from their edges natively
            grip_row = QHBoxLayout()
            grip_row.addStretch(1)
            grip = QSizeGrip(self)
            grip.setFixedSize(14, 14)
            grip_row.addWidget(grip)
            grip_row.setContentsMargins(0, 0, 0, 0)
            outer.addLayout(grip_row)

    def _fit_band(self):
        """Native: keep Qt content just below the title bar / toolbar band (its height changes
        once toolbar items with labels are added)."""
        if not self.native or self.layout() is None:
            return
        band = design._mac().titlebar_height(self)
        band = band or (design.TOOLBAR_HEIGHT if self.has_toolbar else design.TITLEBAR_HEIGHT)
        if self.layout().contentsMargins().top() != band:
            self.layout().setContentsMargins(0, band, 0, 0)

    def set_subtitle(self, text: str):
        """Second line of the title (file name and size, like a document's subtitle)."""
        if self.native:
            design._mac().set_subtitle(self, text)
        else:
            self._subtitle_label.setText(text)
            self._subtitle_label.setVisible(bool(text))

    def _restyle(self):
        self.setStyleSheet(design.stylesheet(self.native))
        self.update()

    # ---- window behaviour
    def present(self):
        """Centre on the cursor's screen, capture what's behind (glass only), then show."""
        from PySide6.QtGui import QCursor, QGuiApplication
        scr = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(min(self.width(), scr.width() - 40), min(self.height(), scr.height() - 40))
        self.move(scr.center() - self.rect().center())
        self._placed = True
        if not self.native:
            self.liquid.capture_at(self.frameGeometry())
        self.show()
        if sys.platform == "darwin":
            from . import platform_mac
            platform_mac.activate()   # menu-bar apps aren't brought forward automatically
        self.raise_()
        self.activateWindow()

    def showEvent(self, e):
        super().showEvent(e)
        if self.native:
            self._fit_band()
            return
        if not getattr(self, "_placed", False):
            scr = self.screen().availableGeometry()
            self.move(scr.center() - self.rect().center())
            self._placed = True
            self.liquid.capture_at(self.frameGeometry())
        self.liquid.excluded = glass.exclude_from_capture(int(self.winId()))

    def moveEvent(self, e):
        super().moveEvent(e)
        self._stale()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.native:
            self._fit_band()
        else:
            glass.apply_shape(self, glass.rounded_rect_path(QRectF(self.rect()), self.radius))
        self._stale()

    def _stale(self):
        if self.native or not self.isVisible():
            return
        if self.liquid.excluded:
            self.liquid.moved()
        elif self.liquid.image is not None:
            # can't re-capture behind ourselves: switch to the system blur instead
            self.liquid.image = None
            if IS_WIN and not self._acrylic:
                self._acrylic = glass.enable_acrylic(int(self.winId()), 0x40F6F6F6 if not design.is_dark() else 0x40282828)
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        band = design.TOOLBAR_HEIGHT if self.has_toolbar else 40
        if e.button() == Qt.LeftButton and e.position().y() < band:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return  # native move: snapping etc.
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.native:
            if not self._vibrant:
                p.fillRect(self.rect(), design.color("windowBackground"))
            return
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = glass.rounded_rect_path(r, self.radius)
        img = self.liquid.image
        if design.reduce_transparency():
            p.fillPath(path, design.color("windowBackground"))
            p.setPen(design.color("separator"))
            p.drawPath(path)
        elif img is not None and not img.isNull() and img.size() == self.size():
            p.drawImage(0, 0, img)
            # large surfaces need more opacity than a menu so content stays readable
            glass.paint_glass(p, path, tint=design.color("glassTint"), rainbow_alpha=0)
        else:
            tint = design.color("glassTint" if self._acrylic else "glassTintSolid")
            glass.paint_glass(p, path, tint=tint, rainbow_alpha=0)


class ImageView(QWidget):
    """Pages-style canvas: the image sits like a page on a grey canvas with a soft shadow,
    scaled to fit, with helpers to map between widget and image coords.
    Subclasses override `paint_overlay` and the mouse hooks."""

    changed = Signal()
    MARGIN = 28

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.corner_radius = 0.0   # set by the window to follow its rounded corner
        self.set_image(image)

    def set_image(self, image: QImage):
        self.image = image
        self.pix = QPixmap.fromImage(image)
        self.update()

    # geometry
    def target_rect(self) -> QRectF:
        if self.image.isNull():
            return QRectF()
        m = self.MARGIN
        avail = QRectF(self.rect()).adjusted(m, m, -m, -m)
        s = min(avail.width() / self.image.width(), avail.height() / self.image.height())
        w, h = self.image.width() * s, self.image.height() * s
        return QRectF(avail.center().x() - w / 2, avail.center().y() - h / 2, w, h)

    def scale(self) -> float:
        t = self.target_rect()
        return t.width() / max(1, self.image.width())

    def to_image(self, pt: QPointF) -> QPointF:
        t = self.target_rect()
        s = self.scale() or 1
        return QPointF((pt.x() - t.left()) / s, (pt.y() - t.top()) / s)

    def to_widget(self, pt: QPointF) -> QPointF:
        t = self.target_rect()
        s = self.scale()
        return QPointF(t.left() + pt.x() * s, t.top() + pt.y() * s)

    def rect_to_widget(self, r: QRectF) -> QRectF:
        a = self.to_widget(r.topLeft())
        b = self.to_widget(r.bottomRight())
        return QRectF(a, b)

    def clamp_image(self, pt: QPointF) -> QPointF:
        return QPointF(min(max(pt.x(), 0), self.image.width()), min(max(pt.y(), 0), self.image.height()))

    def paint_canvas(self, p: QPainter):
        paint_canvas(p, QRectF(self.rect()), self.corner_radius)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        self.paint_canvas(p)
        t = self.target_rect()
        page_shadow(p, t)
        p.drawPixmap(t, self.pix, QRectF(self.pix.rect()))
        self.paint_overlay(p, t)

    def paint_overlay(self, p: QPainter, t: QRectF):
        pass


def paint_canvas(p: QPainter, r: QRectF, corner_radius: float = 0.0):
    """The grey document canvas behind a page; `corner_radius` rounds the corner that meets
    the window's rounded corner."""
    if corner_radius:
        path = QPainterPath()
        path.addRoundedRect(r, corner_radius, corner_radius)
        # only the bottom-left corner meets the window's rounded corner
        square = QPainterPath()
        square.addRect(r.adjusted(corner_radius, 0, 0, 0))
        square.addRect(r.adjusted(0, 0, 0, -corner_radius))
        p.fillPath(path.united(square), design.color("canvas"))
    else:
        p.fillRect(r, design.color("canvas"))


def page_shadow(p: QPainter, t: QRectF):
    """Pages' soft page shadow: a few stacked translucent rects (cheap, no blur pass)."""
    if t.isEmpty():
        return
    p.save()
    p.setPen(Qt.NoPen)
    base = design.color("shadow")
    for grow, a in ((6, 0.10), (3.5, 0.18), (1.5, 0.28), (0.5, 0.5)):
        c = QColor(base)
        c.setAlphaF(base.alphaF() * a)
        p.setBrush(c)
        p.drawRoundedRect(t.adjusted(-grow, -grow + 1.5, grow, grow + 1.5), grow + 1, grow + 1)
    p.restore()
