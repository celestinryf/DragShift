"""Float: pin dragged items in a small always-on-top panel, to drag them out again later
(into a mail, a chat, another folder...). A panel only holds paths: it never moves, copies
or changes the files, and dragging out offers a copy, never a move."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileInfo, QMimeData, QPoint, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (QCursor, QDrag, QFontMetrics, QGuiApplication, QImageReader, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QFileIconProvider, QWidget

from . import controls, design, glass
from .window import page_shadow

W, H = 132, 156
THUMB = 72            # thumbnail / icon box, in points
CLOSE = QRectF(6, 6, 20, 20)
CASCADE = 24          # offset between panels opened at the same spot


def thumbnail(path: str | Path, side: int, dpr: float = 2.0) -> tuple[QPixmap, bool]:
    """(pixmap, is_picture): a thumbnail for images Qt can read, otherwise the system's icon
    for the file or folder."""
    px = int(side * dpr)
    reader = QImageReader(str(path))
    if not Path(path).is_dir() and reader.canRead():
        size = reader.size()
        if size.isValid() and not size.isEmpty():
            reader.setScaledSize(size.scaled(QSize(px, px), Qt.KeepAspectRatio))
        img = reader.read()
        if not img.isNull():
            pm = QPixmap.fromImage(img)
            pm.setDevicePixelRatio(dpr)
            return pm, True
    return QFileIconProvider().icon(QFileInfo(str(path))).pixmap(QSize(side, side), dpr), False


class FloatPanel(QWidget):
    """One floating panel: thumbnail, name, close button. Drag from the picture to drop the
    items anywhere; drag the strip along the top to move the panel."""
    closed = Signal(object)
    moved = Signal(object)

    def __init__(self, paths: list[str]):
        super().__init__(None)
        self.paths = [str(p) for p in paths]
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(W, H)
        self.setWindowTitle(self.title)
        self.setAccessibleName(f"Floating {self.title}")
        self.setAccessibleDescription("Drag the picture to drop the items somewhere. Escape closes.")
        self.setToolTip("\n".join(self.paths))
        self.thumb, self.is_picture = thumbnail(self.paths[0], THUMB, max(1.0, self.devicePixelRatioF()))
        self.hovered = False
        self._press: QPoint | None = None     # press on the picture: may become a drag out
        self._moving: QPoint | None = None    # press on the top strip: moves the panel
        self._closing = False                 # press on the close button
        self._vibrant = False
        self.last_drag: QDrag | None = None   # kept for tests and so the drag isn't collected mid-flight

    @property
    def title(self) -> str:
        name = Path(self.paths[0]).name or self.paths[0]
        return name if len(self.paths) == 1 else f"{len(self.paths)} items"

    def missing(self) -> bool:
        return not any(Path(p).exists() for p in self.paths)

    # ------------------------------------------------------------ show
    def present(self, pos: QPoint):
        self.move(pos)
        radius = design.RADIUS["popover"]
        glass.apply_shape(self, glass.rounded_rect_path(QRectF(0, 0, W, H), radius))
        self.show()
        if controls.native():   # the same popover material as the banners
            self._vibrant = design._mac().add_vibrancy(self, "popover", radius=radius, follow_active=False)
        self.raise_()

    # ------------------------------------------------------------ drag out
    def mime(self) -> QMimeData:
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in self.paths if Path(p).exists()])
        return md

    def start_drag(self) -> QDrag | None:
        if self.missing():
            self.update()
            return None
        drag = QDrag(self)
        drag.setMimeData(self.mime())
        if not self.thumb.isNull():
            pm = self.thumb.scaled(self.thumb.size() / 1.5, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(self.thumb.devicePixelRatio())
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(int(pm.deviceIndependentSize().width() / 2),
                                   int(pm.deviceIndependentSize().height() / 2)))
        self.last_drag = drag
        drag.exec(Qt.CopyAction, Qt.CopyAction)   # copy only: the original never moves
        return drag

    # ------------------------------------------------------------ input
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if CLOSE.adjusted(-4, -4, 4, 4).contains(pos):
            self._closing = True
        elif pos.y() < CLOSE.bottom() + 4:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self._moving = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        else:
            self._press = pos.toPoint()

    def mouseMoveEvent(self, e):
        if self._moving is not None:
            self.move(e.globalPosition().toPoint() - self._moving)
        elif self._press is not None and \
                (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
            self._press = None
            self.start_drag()

    def mouseReleaseEvent(self, e):
        close = self._closing and CLOSE.adjusted(-4, -4, 4, 4).contains(e.position())
        self._press = self._moving = None
        self._closing = False
        if close:
            self.close()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Delete, Qt.Key_Backspace):
            self.close()
        else:
            super().keyPressEvent(e)

    def enterEvent(self, e):
        self.hovered = True
        self.update()

    def leaveEvent(self, e):
        self.hovered = False
        self.update()

    def moveEvent(self, e):
        super().moveEvent(e)
        self.moved.emit(self)

    def closeEvent(self, e):
        self.closed.emit(self)
        super().closeEvent(e)

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        radius = design.RADIUS["popover"]
        path = glass.rounded_rect_path(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius)
        if not self._vibrant:
            fill = "windowBackground" if design.reduce_transparency() else "glassTintSolid"
            glass.paint_glass(p, path, tint=design.color(fill), rainbow_alpha=0, highlight=0.6)
        p.setPen(QPen(design.color("separator", alpha=1.4), design.HAIRLINE))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        gone = self.missing()
        box = QRectF((W - THUMB) / 2, 30, THUMB, THUMB)
        if len(self.paths) > 1:   # a stack: two offset cards behind the picture
            for k in (2, 1):
                p.setPen(QPen(design.color("separator", alpha=2.0), design.HAIRLINE))
                p.setBrush(design.color("controlBackground"))
                p.drawRoundedRect(box.adjusted(4 * k, -4 * k, 4 * k, -4 * k).adjusted(6, 6, -6, -6),
                                  design.RADIUS["small"], design.RADIUS["small"])
        if not self.thumb.isNull():
            sz = self.thumb.deviceIndependentSize()
            k = min(THUMB / max(1, sz.width()), THUMB / max(1, sz.height()), 1.0)
            w, h = sz.width() * k, sz.height() * k
            p.setOpacity(0.35 if gone else 1.0)
            target = QRectF(box.center().x() - w / 2, box.center().y() - h / 2, w, h)
            if self.is_picture:   # a photo sits like a page, with Pages' soft shadow
                page_shadow(p, target)
            p.drawPixmap(target, self.thumb, QRectF(self.thumb.rect()))
            p.setOpacity(1.0)

        f = design.font("caption1", 500)
        p.setFont(f)
        p.setPen(design.color("label"))
        fm = QFontMetrics(f)
        text = box.bottom() + design.SPACING["s"]
        name = fm.elidedText(self.title, Qt.ElideMiddle, W - 2 * design.SPACING["m"])
        p.drawText(QRectF(design.SPACING["m"], text, W - 2 * design.SPACING["m"], 16), Qt.AlignCenter, name)
        sub = "Moved or deleted" if gone else "Drag to use"
        p.setPen(design.color("orangeText" if gone else "secondaryLabel"))
        p.drawText(QRectF(design.SPACING["m"], text + 15, W - 2 * design.SPACING["m"], 15), Qt.AlignCenter, sub)

        if self.hovered or self.hasFocus():
            controls.glass_capsule(p, CLOSE, strong=True)
            pm = design.symbol("xmark", 8, design.color("secondaryLabel"), "bold", fallback="✕")
            if pm is not None:
                sz = pm.deviceIndependentSize()
                p.drawPixmap(QPointF(CLOSE.center().x() - sz.width() / 2, CLOSE.center().y() - sz.height() / 2), pm)


class Floats:
    """Keeps every open panel alive and remembers, for this session, where each set of
    items was floated so reopening it puts the panel back in the same place."""

    def __init__(self):
        self.panels: list[FloatPanel] = []
        self.positions: dict[tuple[str, ...], QPoint] = {}

    def open(self, paths: list[str], at: QPoint | None = None) -> FloatPanel:
        panel = FloatPanel(paths)
        key = tuple(panel.paths)
        pos = self.positions.get(key) or self._free_spot(at or QCursor.pos())
        panel.closed.connect(self._closed)
        panel.moved.connect(lambda w: self.positions.__setitem__(tuple(w.paths), w.pos()))
        self.panels.append(panel)
        panel.present(pos)
        self.positions[key] = panel.pos()
        return panel

    def _free_spot(self, near: QPoint) -> QPoint:
        """Just right of the cursor, on screen, stepping down past panels already there."""
        scr = (QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()).availableGeometry()
        pos = QPoint(near.x() + 16, near.y() - H // 2)
        taken = {w.pos() for w in self.panels}
        while pos in taken:
            pos += QPoint(CASCADE, CASCADE)
        pos.setX(min(max(pos.x(), scr.left()), scr.right() - W))
        pos.setY(min(max(pos.y(), scr.top()), scr.bottom() - H))
        return pos

    def _closed(self, panel: FloatPanel):
        if panel in self.panels:
            self.panels.remove(panel)
