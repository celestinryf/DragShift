"""Notifications styled as macOS notification banners (top-right on macOS, with the real
banner material; bottom-right glass cards elsewhere), stacked.

A card appears immediately while work runs (spinner), then turns into the result:
thumbnail, what changed (e.g. "2.4 MB → 610 KB · 75% smaller") and actions:
Open · Show in Finder/folder · Undo (moves the new files to the Trash / Recycle Bin)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QFile, QPointF, QRect, QRectF, Qt, QTimer, QUrl,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QCursor, QDesktopServices, QFontMetrics,
                           QGuiApplication, QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QWidget

from . import controls, design, glass
from .report import Result
from .util import plural

W, H = 356, 96        # macOS banner width; tall enough for two body lines + actions
GAP = 8
MARGIN = 16           # from the screen edge (macOS: right edge, below the menu bar)
PAD = 14              # inside the banner
LEAD = 36             # leading image (thumbnail / symbol), like a banner's app icon
CLOSE = QRectF(4, 4, 20, 20)   # banner close button, shown on hover (top-left, as on macOS)


def reveal(path: str | Path):
    path = str(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        from . import platform_linux
        if not platform_linux.reveal(path):
            subprocess.Popen(["xdg-open", str(Path(path).parent)])


def open_file(path: str | Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def trash(paths: list[Path], prune: bool = True) -> int:
    """Move results to the Trash. `prune` also removes output folders left empty (only for
    folders DragShift made, like "x images/"; never for a folder the user picked)."""
    n = 0
    for p in paths:
        p = Path(p)
        if p.is_dir() and not p.is_symlink():   # a copied folder: only ever to the Trash
            if QFile.moveToTrash(str(p)):
                n += 1
        elif p.is_file():
            if QFile.moveToTrash(str(p)):
                n += 1
            else:
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
    # remove now-empty output folders we created (e.g. "x images/")
    for d in ({Path(p).parent for p in paths} if prune else ()):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return n


def trash_message(n: int) -> str:
    bin_name = "Recycle Bin" if sys.platform == "win32" else "Trash"
    return f"Moved {plural(n, 'item')} to the {bin_name}"


class ToastCard(QWidget):
    closed = Signal(object)
    cancel_requested = Signal()   # the user closed a card while its job was still working

    def __init__(self, stack: "ToastStack"):
        super().__init__(None)
        self.stack = stack
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.resize(W, H)
        self.state = "working"     # working | done | error | info
        self.title = ""
        self.detail = ""
        self.good = ""
        self.result: Result | None = None
        self.thumb: QPixmap | None = None
        self.spin = 0.0
        self.hover_btn = -1
        self.buttons: list[tuple[str, QRectF]] = []
        self.hovered = False
        self._fade = 0.0
        self._vibrant = False
        from .liquid import GlassStyle
        self.liquid = glass.LiquidBacking(self, 20, GlassStyle(blur=8, strength=12, bezel=16))
        self.liquid.changed.connect(self.update)
        self.fade = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=design.duration("standard"),
                                      easingCurve=QEasingCurve.OutCubic)
        self.fade.valueChanged.connect(self._set_fade)
        self.spinner = QTimer(self, interval=16, timeout=self._spin)
        self.life = QTimer(self, singleShot=True, timeout=self.dismiss)

    # ------------------------------------------------------------ state
    def working(self, title: str, detail: str = ""):
        self.state, self.title, self.detail = "working", title, detail
        self.spinner.start()
        self.update()

    def done(self, r: Result):
        self.spinner.stop()
        self.state, self.result = "done", r
        self.title, self.detail, self.good = r.title, r.detail, r.good
        self.thumb = None
        if r.thumb is not None:
            pm = QPixmap(str(r.thumb))
            if not pm.isNull():
                px = int(LEAD * self.devicePixelRatioF())
                self.thumb = pm.scaled(px, px, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self.life.start(6500)
        self.update()

    def error(self, title: str, detail: str):
        self.spinner.stop()
        self.state, self.title, self.detail = "error", title, detail
        self.life.start(9000)
        self.update()

    def info(self, title: str, detail: str, ms: int = 4500):
        self.spinner.stop()
        self.state, self.title, self.detail = "info", title, detail
        self.life.start(ms)
        self.update()

    def _spin(self):
        self.spin = (self.spin + 7) % 360
        self.update()

    def _set_fade(self, v):
        self._fade = float(v)
        self.setWindowOpacity(self._fade)
        self.update()

    def appear(self, rect: QRect):
        self.setGeometry(rect)
        radius = design.RADIUS["banner"]
        glass.apply_shape(self, glass.rounded_rect_path(QRectF(0, 0, W, H), radius))
        native = controls.native()
        if not native:
            self.liquid.capture_at(rect)
        self.setWindowOpacity(0.0)
        self.show()
        if native:   # the real banner material: live blur, light/dark, Reduce transparency
            self._vibrant = design._mac().add_vibrancy(self, "popover", radius=radius, follow_active=False)
        else:
            self.liquid.excluded = glass.exclude_from_capture(int(self.winId()))
        self.fade.setDuration(design.duration("standard"))
        self.fade.start()

    def relocate(self, rect: QRect):
        if rect == self.geometry():
            return
        self.setGeometry(rect)
        if self._vibrant:
            return
        if self.liquid.excluded:
            self.liquid.refresh()
        else:
            self.liquid.image = None
            self.update()

    def dismiss(self):
        self.spinner.stop()
        self.life.stop()
        self.hide()
        self.closed.emit(self)
        self.deleteLater()

    # ------------------------------------------------------------ input
    def enterEvent(self, e):
        self.life.stop()
        self.hovered = True
        self.update()

    def leaveEvent(self, e):
        self.hover_btn = -1
        self.hovered = False
        if self.state in ("done", "info"):
            self.life.start(2500)
        elif self.state == "error":
            self.life.start(4000)
        self.update()

    def mouseMoveEvent(self, e):
        hb = -1
        for i, (_, r) in enumerate(self.buttons):
            if r.contains(e.position()):
                hb = i
        if hb != self.hover_btn:
            self.hover_btn = hb
            self.update()

    def mouseReleaseEvent(self, e):
        if CLOSE.adjusted(-4, -4, 4, 4).contains(e.position()):
            if self.state == "working":
                self.cancel_requested.emit()
            self.dismiss()
            return
        for name, r in self.buttons:
            if r.contains(e.position()):
                self._act(name)
                return
        if self.state == "done" and self.result and self.result.outs:
            reveal(self.result.outs[0])
            self.dismiss()

    def _act(self, name: str):
        r = self.result
        if not r:
            return
        if name == "Open":
            open_file(r.outs[0])
            self.dismiss()
        elif name == "Show":
            reveal(r.outs[0])
            self.dismiss()
        elif name == "Undo":
            detail = r.undo() if r.undo is not None else trash_message(trash(r.outs))
            self.result = None
            self.thumb = None
            self.good = ""
            self.info("Undone", detail, 2500)

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        radius = design.RADIUS["banner"]
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = glass.rounded_rect_path(r, radius)
        self._paint_material(p, path)

        lead = QRectF(PAD, PAD, LEAD, LEAD)
        self._paint_lead(p, lead)
        x = lead.right() + 10
        tw = W - x - PAD

        # title, with the result figure (e.g. "75% smaller") where a banner shows its time
        f = design.font("headline")
        fm = QFontMetrics(f)
        good_w = 0
        if self.good:
            fg = design.font("subheadline", 600)
            good_w = QFontMetrics(fg).horizontalAdvance(self.good) + 8
            p.setFont(fg)
            p.setPen(design.color("greenText"))
            p.drawText(QRectF(W - PAD - good_w, PAD, good_w, 17), Qt.AlignRight | Qt.AlignVCenter, self.good)
        p.setFont(f)
        p.setPen(design.color("label"))
        p.drawText(QRectF(x, PAD, tw - good_w, 17), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self.title, Qt.ElideRight, int(tw - good_w)))

        has_actions = self.state == "done" and self.result and self.result.outs
        fb = design.font("body")
        p.setFont(fb)
        p.setPen(design.color("label" if not has_actions else "secondaryLabel"))
        lines = 1 if has_actions else 2
        body = QFontMetrics(fb).elidedText(self.detail, Qt.ElideMiddle, int(tw * lines * 0.97))
        p.drawText(QRectF(x, PAD + 18, tw, 17 * lines), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, body)

        self.buttons = []
        if has_actions:
            self._paint_actions(p, x)
        if self.hovered:
            self._paint_close(p)

    def _paint_material(self, p: QPainter, path: QPainterPath):
        if self._vibrant:     # NSVisualEffectView behind us does the material
            p.setPen(QPen(design.color("separator", alpha=1.4), design.HAIRLINE))
            p.drawPath(path)
            return
        img = self.liquid.image
        if design.reduce_transparency():
            p.fillPath(path, design.color("windowBackground"))
        elif img is not None and not img.isNull() and img.size() == self.size():
            p.drawImage(0, 0, img)
            glass.paint_glass(p, path, tint=design.color("glassTint"), rainbow_alpha=0, highlight=0.6)
        else:
            glass.paint_glass(p, path, tint=design.color("glassTintSolid"), rainbow_alpha=0, highlight=0.6)
        p.setPen(QPen(design.color("separator", alpha=1.4), design.HAIRLINE))
        p.drawPath(path)

    def _paint_lead(self, p: QPainter, lead: QRectF):
        if self.state == "working":
            controls.paint_spinner(p, lead.center(), 11, self.spin / 360, design.color("secondaryLabel"))
        elif self.thumb is not None:
            clip = QPainterPath()
            clip.addRoundedRect(lead, design.RADIUS["group"], design.RADIUS["group"])
            dpr = self.devicePixelRatioF()
            side = int(LEAD * dpr)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(lead, self.thumb, QRectF((self.thumb.width() - side) / 2, (self.thumb.height() - side) / 2,
                                                  side, side))
            p.restore()
            p.setPen(QPen(design.color("separator", alpha=2.0), design.HAIRLINE))
            p.setBrush(Qt.NoBrush)
            p.drawPath(clip)
        else:
            sym, glyph, role = {"done": ("checkmark.circle.fill", "✓", "green"),
                                "error": ("exclamationmark.triangle.fill", "!", "orange"),
                                "info": ("info.circle.fill", "i", "accent")}.get(self.state, ("checkmark.circle.fill", "✓", "green"))
            if self.state == "info" and self.title == "Undone":
                sym = "arrow.uturn.backward.circle.fill"
            pm = design.symbol(sym, 26, design.color(role), fallback=glyph)
            if pm is not None:
                sz = pm.deviceIndependentSize()
                p.drawPixmap(QPointF(lead.center().x() - sz.width() / 2, lead.center().y() - sz.height() / 2), pm)

    def _paint_actions(self, p: QPainter, x: float):
        names = ["Open", "Show", "Undo"]
        show = "Show in Finder" if sys.platform == "darwin" else "Show in folder"
        labels = {"Open": "Open", "Show": show, "Undo": "Undo"}
        f = design.font("callout", 500)
        p.setFont(f)
        fm = QFontMetrics(f)
        bx = x
        for i, nme in enumerate(names):
            bw = fm.horizontalAdvance(labels[nme]) + 20
            br = QRectF(bx, H - PAD - 22, bw, 22)
            self.buttons.append((nme, br))
            controls.glass_capsule(p, br, strong=self.hover_btn == i)
            if self.hover_btn == i:
                p.setPen(Qt.NoPen)
                p.setBrush(design.color("label", alpha=0.08))
                p.drawRoundedRect(br, br.height() / 2, br.height() / 2)
            p.setPen(design.color("label"))
            p.drawText(br, Qt.AlignCenter, labels[nme])
            bx += bw + 6

    def _paint_close(self, p: QPainter):
        controls.glass_capsule(p, CLOSE, strong=True)
        pm = design.symbol("xmark", 8, design.color("secondaryLabel"), "bold", fallback="✕")
        if pm is not None:
            sz = pm.deviceIndependentSize()
            p.drawPixmap(QPointF(CLOSE.center().x() - sz.width() / 2, CLOSE.center().y() - sz.height() / 2), pm)


class ToastStack:
    """Owns the cards and keeps them stacked bottom-right of the active screen."""

    def __init__(self):
        self.cards: list[ToastCard] = []

    def _screen_rect(self) -> QRect:
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def _slot(self, i: int) -> QRect:
        g = self._screen_rect()
        x = g.right() - W - MARGIN
        if sys.platform == "darwin":   # banners sit under the menu bar, stacking down
            y = g.top() + 10 + i * (H + GAP)
        else:
            y = g.bottom() - MARGIN - H - i * (H + GAP)
        return QRect(x, y, W, H)

    def new(self) -> ToastCard:
        c = ToastCard(self)
        c.closed.connect(self._closed)
        while len(self.cards) >= 4:
            self.cards[0].dismiss()
        self.cards.append(c)
        c.appear(self._slot(len(self.cards) - 1))
        return c

    def _closed(self, card):
        if card in self.cards:
            self.cards.remove(card)
        for i, c in enumerate(self.cards):
            c.relocate(self._slot(i))

    # conveniences
    def info(self, title: str, detail: str, ms: int = 4500) -> ToastCard:
        c = self.new()
        c.info(title, detail, ms)
        return c

    def error(self, title: str, detail: str) -> ToastCard:
        c = self.new()
        c.error(title, detail)
        return c
