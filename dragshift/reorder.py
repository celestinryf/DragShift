"""PDF page reorder: a grid of page thumbnails to drag into a new order, with delete, move
and reverse, saved as a new PDF beside the original.

Thumbnails render a few at a time on the UI thread's idle timer, so a long PDF opens at once
with blank pages that fill in."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
                               QPushButton, QStyledItemDelegate, QStyleOptionViewItem)

from . import design
from .convert import atomic_save, open_pdf
from .tools import ToolWindow
from .util import UserError, plural, sibling
from .window import paint_canvas

THUMB = 132           # longest side of a page thumbnail, in points
BATCH = 6             # thumbnails rendered per timer tick
ORIGINAL = Qt.UserRole + 1


def reorder_pdf(src: str | Path, order: list[int]) -> Path:
    """A new PDF with the pages of `src` in `order` (0-based original page numbers; pages left
    out are dropped)."""
    with open_pdf(src) as doc:
        n = doc.page_count
        if not order:
            raise UserError("A PDF needs at least one page.")
        if any(not 0 <= i < n for i in order):
            raise ValueError(f"Page numbers must be between 0 and {n - 1}")
        doc.select(order)
        out = sibling(src, "Reordered", ".pdf")
        atomic_save(lambda dst: doc.save(str(dst), garbage=3, deflate=True), out)
    return out


class _PageDelegate(QStyledItemDelegate):
    """Page number under its thumbnail (ListMode would put it beside)."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.decorationPosition = QStyleOptionViewItem.Top
        option.displayAlignment = Qt.AlignHCenter | Qt.AlignTop


class PageGrid(QListWidget):
    """Thumbnails in a wrapping grid. Dragging moves pages (the model's row order is the page
    order); the grey canvas is painted behind them like the other tool windows."""

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.ListMode)       # ListMode reorders on drop; IconMode only moves
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setUniformItemSizes(True)
        self.setSpacing(design.SPACING["m"])
        self.setIconSize(QSize(THUMB, THUMB))
        self.setGridSize(QSize(THUMB + 28, THUMB + 40))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setMovement(QListView.Snap)
        self.setFrameShape(QListWidget.NoFrame)
        self.setAccessibleName("Pages")
        self.setItemDelegate(_PageDelegate(self))
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QListWidget { background: transparent; }")
        self.corner_radius = 0.0

    def paintEvent(self, e):
        p = QPainter(self.viewport())
        paint_canvas(p, self.viewport().rect().toRectF(), self.corner_radius)
        p.end()
        super().paintEvent(e)


class ReorderWindow(ToolWindow):
    tool_name = "Reorder"

    def __init__(self, path, notify):
        with open_pdf(path) as doc:
            self.sizes = [(pg.rect.width, pg.rect.height) for pg in doc]
        if not self.sizes:
            raise UserError("This PDF has no pages.")
        super().__init__("Reorder Pages", path, notify, size=(900, 640))
        self.grid = PageGrid()
        self.set_canvas(self.grid)
        self.grid.model().rowsMoved.connect(lambda *_: self._changed())

        g = self.inspector.group("Pages", collapsible=False)
        self.count = QLabel()
        self.count.setObjectName("dim")
        self.count.setWordWrap(True)
        g.addWidget(self.count)
        row = QHBoxLayout()
        row.setSpacing(design.SPACING["s"])
        self.earlier = QPushButton("Move Earlier")
        self.later = QPushButton("Move Later")
        self.earlier.clicked.connect(lambda: self.move_selected(-1))
        self.later.clicked.connect(lambda: self.move_selected(1))
        row.addWidget(self.earlier)
        row.addWidget(self.later)
        g.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(design.SPACING["s"])
        self.delete_btn = QPushButton("Delete Page")
        self.delete_btn.clicked.connect(self.delete_selected)
        reverse = QPushButton("Reverse Order")
        reverse.clicked.connect(self.reverse)
        row.addWidget(self.delete_btn)
        row.addWidget(reverse)
        g.addLayout(row)
        tip = QLabel("Drag pages to reorder them. ⌫ deletes the selected pages; ⌘← and ⌘→ move them."
                     if design.IS_MAC else
                     "Drag pages to reorder them. Delete removes the selected pages; Ctrl+Left and "
                     "Ctrl+Right move them.")
        tip.setObjectName("dim")
        tip.setWordWrap(True)
        g.addWidget(tip)
        self.finish("Save PDF", self.save, self.reset)

        for key in (Qt.Key_Delete, Qt.Key_Backspace):
            QShortcut(QKeySequence(key), self.grid, activated=self.delete_selected)
        QShortcut(QKeySequence(Qt.CTRL | Qt.Key_Left), self.grid, activated=lambda: self.move_selected(-1))
        QShortcut(QKeySequence(Qt.CTRL | Qt.Key_Right), self.grid, activated=lambda: self.move_selected(1))
        self.grid.itemSelectionChanged.connect(self._changed)

        self._pending: list[int] = []
        self._timer = QTimer(self, interval=0, timeout=self._render_some)
        self.reset()

    # ------------------------------------------------------------ pages
    def _placeholder(self, w: float, h: float) -> QIcon:
        k = THUMB / max(w, h)
        pm = QPixmap(max(1, round(w * k)), max(1, round(h * k)))
        pm.fill(QColor("white"))
        return QIcon(pm)

    def _render_some(self):
        """Render the next few thumbnails; items are found by page number, so pages the user
        already moved still get theirs."""
        if not self._pending:
            self._timer.stop()
            return
        batch, self._pending = self._pending[:BATCH], self._pending[BATCH:]
        items = {self.grid.item(r).data(ORIGINAL): self.grid.item(r) for r in range(self.grid.count())}
        try:
            with open_pdf(self.path) as doc:
                for i in batch:
                    if i not in items:
                        continue
                    page = doc[i]
                    k = THUMB * self.devicePixelRatioF() / max(page.rect.width, page.rect.height)
                    pix = page.get_pixmap(matrix=_matrix(k), alpha=False)
                    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
                    pm = QPixmap.fromImage(img)
                    pm.setDevicePixelRatio(self.devicePixelRatioF())
                    items[i].setIcon(QIcon(pm))
        except (UserError, RuntimeError, ValueError):
            self._pending = []      # the file changed or went away: keep the blank pages

    def order(self) -> list[int]:
        """Original page numbers (0-based) in the order shown."""
        return [self.grid.item(r).data(ORIGINAL) for r in range(self.grid.count())]

    def reset(self):
        self.grid.clear()
        for i, (w, h) in enumerate(self.sizes):
            item = QListWidgetItem(self._placeholder(w, h), str(i + 1))
            item.setData(ORIGINAL, i)
            item.setTextAlignment(Qt.AlignHCenter)
            item.setToolTip(f"Page {i + 1}")
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
            self.grid.addItem(item)
        self._pending = list(range(len(self.sizes)))
        self._timer.start()
        self._changed()

    def _selected_rows(self) -> list[int]:
        return sorted(self.grid.row(it) for it in self.grid.selectedItems())

    def move_selected(self, step: int):
        rows = self._selected_rows()
        if not rows or (step < 0 and rows[0] == 0) or (step > 0 and rows[-1] == self.grid.count() - 1):
            return
        order = list(range(self.grid.count()))
        for r in (rows if step < 0 else reversed(rows)):
            order[r], order[r + step] = order[r + step], order[r]
        items = [self.grid.takeItem(0) for _ in range(self.grid.count())]
        for r in order:
            self.grid.addItem(items[r])
        for r in rows:
            self.grid.item(r + step).setSelected(True)
        self._changed()

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        if len(rows) >= self.grid.count():
            self.count.setText("A PDF needs at least one page, so the last one can't be deleted.")
            return
        for r in reversed(rows):
            self.grid.takeItem(r)
        self._changed()

    def reverse(self):
        items = [self.grid.takeItem(0) for _ in range(self.grid.count())]
        for it in reversed(items):
            self.grid.addItem(it)
        self._changed()

    def _changed(self):
        n, total = self.grid.count(), len(self.sizes)
        removed = total - n
        text = plural(n, "page")
        if removed:
            text += f" · {removed} deleted"
        self.count.setText(text)
        selected = bool(self.grid.selectedItems())
        self.earlier.setEnabled(selected)
        self.later.setEnabled(selected)
        self.delete_btn.setEnabled(selected and len(self.grid.selectedItems()) < n)
        if self.primary_btn is not None:
            self.primary_btn.setEnabled(self.order() != list(range(total)))

    def save(self):
        order, path = self.order(), self.path
        removed = len(self.sizes) - len(order)
        extra = f"{plural(removed, 'page')} deleted" if removed else ""
        self.run(lambda: [reorder_pdf(path, order)], "Saving pages…", extra)


def _matrix(k: float):
    from .convert import fitz
    return fitz.Matrix(k, k)
