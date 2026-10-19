"""Pages-style controls drawn from the design tokens: segmented controls, an inspector with
collapsible groups, icon-above-label toolbar buttons in Liquid Glass capsules, colour wells,
popovers with an arrow, hairline separators and the macOS spoke spinner.

On macOS, standard controls (push buttons, pop-ups, sliders, steppers, check boxes) are left
to Qt's native style, which draws real AppKit controls; only what AppKit has no Qt widget
for is drawn here."""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from . import design


def native() -> bool:
    """Real AppKit chrome is available (macOS, Cocoa platform, AppKit importable)."""
    app = QGuiApplication.instance()
    return design._mac() is not None and app is not None and app.platformName() == "cocoa"


def glass_capsule(p: QPainter, r: QRectF, radius: float | None = None, strong: bool = False):
    """A Liquid Glass control background (Tahoe toolbar groups, segmented tracks, banner
    buttons): a translucent fill with a bright top edge and a hairline. Solid with Reduce
    transparency."""
    dark = design.is_dark()
    radius = r.height() / 2 if radius is None else radius
    if design.reduce_transparency():
        fill = design.color("controlBackground") if not dark else QColor(70, 70, 70)
    elif dark:
        fill = QColor(255, 255, 255, 34 if strong else 22)
    else:
        fill = QColor(255, 255, 255, 220 if strong else 170)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    if not dark:   # soft contact shadow under light glass
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 14))
        p.drawRoundedRect(r.translated(0, 1), radius, radius)
    p.setBrush(fill)
    edge = QLinearGradient(r.topLeft(), r.bottomLeft())
    edge.setColorAt(0, QColor(255, 255, 255, 90 if dark else 255))
    edge.setColorAt(0.5, QColor(255, 255, 255, 18 if dark else 120))
    edge.setColorAt(1, QColor(255, 255, 255, 30 if dark else 160))
    p.setPen(QPen(QBrush(edge), design.HAIRLINE))
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.setPen(QPen(design.color("separator", alpha=0.9 if dark else 0.7), design.HAIRLINE))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.restore()


def focus_ring(p: QPainter, r: QRectF, radius: float):
    """macOS keyboard focus ring: 3-pt accent halo just outside the control."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(design.color("focusRing"), 3))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), radius + 1.5, radius + 1.5)
    p.restore()


def paint_spinner(p: QPainter, center: QPointF, radius: float, phase: float, col: QColor):
    """The macOS spinning progress indicator: 12 spokes fading round. `phase` in 0..1."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    lead = int(phase * 12) % 12
    for i in range(12):
        age = (lead - i) % 12
        c = QColor(col)
        c.setAlphaF(col.alphaF() * max(0.18, 1 - age / 12))
        a = math.radians(90 - i * 30)
        p.setPen(QPen(c, max(1.5, radius * 0.2), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(center.x() + math.cos(a) * radius * 0.5, center.y() - math.sin(a) * radius * 0.5),
                   QPointF(center.x() + math.cos(a) * radius, center.y() - math.sin(a) * radius))
    p.restore()


def paint_swatch(p: QPainter, r: QRectF, colors: list[str], radius: float):
    """A colour or gradient chip (colour wells, background presets)."""
    if len(colors) == 1:
        brush = QBrush(QColor(colors[0]))
    else:
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        for i, c in enumerate(colors):
            g.setColorAt(i / (len(colors) - 1), QColor(c))
        brush = QBrush(g)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(design.color("separator", alpha=2.0), design.HAIRLINE))
    p.setBrush(brush)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.restore()


class Hairline(QFrame):
    """A one-pixel separator in the system separator colour."""

    def __init__(self, vertical: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("hairline")
        if vertical:
            self.setFixedWidth(1)
        else:
            self.setFixedHeight(1)


# ---------------------------------------------------------------- segmented control
class _Segment(QPushButton):
    def __init__(self, text: str, parent):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName(text)
        self.setCursor(Qt.ArrowCursor)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self.text().replace("&&", "&")) + 22, self.parent().seg_height - 4)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        if self.isChecked():
            if design.is_dark():
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 60 if not design.reduce_transparency() else 90))
                p.drawRoundedRect(r, rad, rad)
            else:
                glass_capsule(p, r, rad, strong=True)
        elif self.isDown() or self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(design.color("label", alpha=0.06 if not self.isDown() else 0.12))
            p.drawRoundedRect(r, rad, rad)
        f = design.font(self.parent().text_style, 500 if self.isChecked() else 400)
        p.setFont(f)
        p.setPen(design.color("label" if self.isEnabled() else "tertiaryLabel"))
        p.drawText(r, Qt.AlignCenter, self.text().replace("&&", "&"))
        if self.hasFocus():
            focus_ring(p, r.adjusted(1.5, 1.5, -1.5, -1.5), rad - 1.5)


class Segmented(QWidget):
    """A capsule segmented control (one choice). `group` is a plain QButtonGroup, so callers
    use idClicked / checkedId / button(i) as with any exclusive button row."""

    def __init__(self, labels: list[str], checked: int = 0, small: bool = False, expand: bool = False,
                 parent=None):
        super().__init__(parent)
        self.seg_height = design.CONTROL_HEIGHT["small" if small else "regular"] + 2
        self.text_style = "inspector" if small else "body"
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(0)
        for i, t in enumerate(labels):
            b = _Segment(t, self)
            b.setFont(design.font(self.text_style))
            b.setChecked(i == checked)
            if expand:
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(b, i)
            row.addWidget(b)
        self.group.buttonToggled.connect(lambda *_: self.update())
        self.setFixedHeight(self.seg_height)
        if expand:
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(" / ".join(labels))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(design.color("label", alpha=0.07 if not design.is_dark() else 0.1))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)


# ---------------------------------------------------------------- inspector
class DisclosureHeader(QAbstractButton):
    """A collapsible group's header: small bold title with a disclosure chevron."""

    def __init__(self, title: str, collapsible: bool, parent=None):
        super().__init__(parent)
        self.setText(title)
        self.setCheckable(collapsible)
        self.setChecked(True)
        self.collapsible = collapsible
        self.setFocusPolicy(Qt.TabFocus if collapsible else Qt.NoFocus)
        self.setAccessibleName(title)
        self.setFixedHeight(22)
        self.setCursor(Qt.ArrowCursor)

    def sizeHint(self) -> QSize:
        return QSize(120, 22)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x = 0
        if self.collapsible:
            name = "chevron.down" if self.isChecked() else "chevron.right"
            pm = design.symbol(name, 9, design.color("secondaryLabel"), "bold",
                               fallback="▾" if self.isChecked() else "▸")
            if pm is not None:
                s = pm.deviceIndependentSize()
                p.drawPixmap(QPointF((12 - s.width()) / 2, (self.height() - s.height()) / 2), pm)
            x = 16
        p.setFont(design.font("inspectorHeader"))
        p.setPen(design.color("label"))
        p.drawText(QRectF(x, 0, self.width() - x, self.height()), Qt.AlignLeft | Qt.AlignVCenter, self.text())
        if self.hasFocus():
            focus_ring(p, QRectF(self.rect()).adjusted(1, 2, -1, -2), design.RADIUS["small"])


SMALL_CONTROLS = (QPushButton, QComboBox, QSpinBox, QLineEdit, QCheckBox, QSlider)


class Inspector(QWidget):
    """Pages' Format inspector: an optional segmented tab bar, then groups with small bold
    headers and disclosure triangles, separated by hairlines. Controls inside use the
    small control size on macOS. `content(page)` is the layout of a tab's page."""

    def __init__(self, tabs: list[str] | None = None, fixed_width: int | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("inspector")
        self.panel = False
        if fixed_width:
            self.setFixedWidth(fixed_width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.tabs: Segmented | None = None
        self.stack = QStackedWidget()
        self._pages: list[QVBoxLayout] = []
        self._groups: list[int] = []
        self.headers: list[DisclosureHeader] = []
        n = len(tabs) if tabs else 1
        if tabs:
            self.tabs = Segmented(tabs, 0, small=True, expand=True)
            wrap = QHBoxLayout()
            wrap.setContentsMargins(design.SPACING["l"], design.SPACING["m"], design.SPACING["l"], design.SPACING["s"])
            wrap.addWidget(self.tabs)
            outer.addLayout(wrap)
            self.tabs.group.idClicked.connect(self.stack.setCurrentIndex)
        for _ in range(n):
            page = QWidget()
            page.setObjectName("inspectorPage")
            lay = QVBoxLayout(page)
            lay.setContentsMargins(design.SPACING["l"], design.SPACING["s"], design.SPACING["l"], design.SPACING["m"])
            lay.setSpacing(design.SPACING["s"])
            self._pages.append(lay)
            self._groups.append(0)
            scroll = QScrollArea()
            scroll.setWidget(page)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
            self.stack.addWidget(scroll)
        outer.addWidget(self.stack, 1)
        self.footer_host = QVBoxLayout()
        self.footer_host.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.footer_host)

    def content(self, page: int = 0) -> QVBoxLayout:
        return self._pages[page]

    def group(self, title: str = "", collapsible: bool = True, page: int = 0, expanded: bool = True) -> QVBoxLayout:
        """Add a group (hairline above all but the first) and return its content layout.
        Its header, if any, is `self.headers[-1]`."""
        lay = self._pages[page]
        if self._groups[page]:
            lay.addSpacing(design.SPACING["xs"])
            lay.addWidget(Hairline())
            lay.addSpacing(design.SPACING["xs"])
        self._groups[page] += 1
        box = QWidget()
        body = QVBoxLayout(box)
        body.setContentsMargins(16 if collapsible and title else 0, 0, 0, 0)
        body.setSpacing(design.SPACING["s"])
        if title:
            head = DisclosureHeader(title, collapsible)
            head.toggled.connect(box.setVisible)
            head.setChecked(expanded)
            box.setVisible(expanded)
            self.headers.append(head)
            lay.addWidget(head)
        lay.addWidget(box)
        return body

    def finish(self):
        """Push groups to the top of each page."""
        for lay in self._pages:
            lay.addStretch(1)
        self._small_controls()

    def make_panel(self, width: int = design.INSPECTOR_WIDTH):
        """Become a fixed-width side panel beside a canvas (small controls, like Pages)."""
        self.setFixedWidth(width)
        self.panel = True
        self._small_controls()

    def _small_controls(self):
        # Pages' inspector panel uses the small control size; set before first show so the
        # layout doesn't shift (and leave stale pixels) once visible
        if not (design.IS_MAC and getattr(self, "panel", False)):
            return
        for w in self.stack.findChildren(QWidget):
            if isinstance(w, SMALL_CONTROLS) and not isinstance(w, (_Segment, ToolbarButton)):
                w.setAttribute(Qt.WA_MacSmallSize)

    def set_footer(self, row):
        self.footer_host.addWidget(Hairline())
        holder = QWidget()
        row.setContentsMargins(design.SPACING["l"], design.SPACING["m"], design.SPACING["l"], design.SPACING["m"])
        holder.setLayout(row)
        self.footer_host.addWidget(holder)


def form_label(text: str) -> QLabel:
    """A row label inside the inspector."""
    lab = QLabel(text)
    lab.setObjectName("inspectorLabel")
    return lab


# ---------------------------------------------------------------- toolbar
class ToolbarButton(QToolButton):
    """Icon above label (Pages' toolbar): an SF Symbol (text glyph off macOS) and a small label."""
    ICON_H = 28
    HEIGHT = ICON_H + 18

    def __init__(self, symbol_name: str, label: str, glyph: str = "", checkable: bool = False, parent=None):
        super().__init__(parent)
        self.symbol_name, self.glyph = symbol_name, glyph
        self.setText(label)
        self.setCheckable(checkable)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.setCursor(Qt.ArrowCursor)
        self.setFixedHeight(self.HEIGHT)
        self.native: tuple | None = None   # (window, group id, index) once mirrored in an NSToolbar

    def set_label(self, label: str):
        """Change the label here and on the native toolbar item."""
        self.setText(label)
        if self.native is not None:
            design._mac().toolbar_set_label(*self.native, label)

    def sizeHint(self) -> QSize:
        f = design.font("toolbarLabel")
        from PySide6.QtGui import QFontMetrics
        w = max(40, QFontMetrics(f).horizontalAdvance(self.text()) + 10)
        return QSize(w, self.HEIGHT)

    def icon_rect(self) -> QRectF:
        w = min(self.width() - 4, 40)
        return QRectF((self.width() - w) / 2, 0, w, self.ICON_H)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        ir = self.icon_rect()
        on = self.isChecked()
        if on or self.isDown() or self.underMouse():
            p.setPen(Qt.NoPen)
            if on:
                p.setBrush(design.color("accent", alpha=0.2))
            else:
                p.setBrush(design.color("label", alpha=0.08 if not self.isDown() else 0.16))
            p.drawRoundedRect(ir, ir.height() / 2, ir.height() / 2)
        role = "accent" if on else ("label" if self.isEnabled() else "tertiaryLabel")
        pm = design.symbol(self.symbol_name, design.TOOLBAR_SYMBOL, design.color(role), "regular",
                           fallback=self.glyph or self.text()[:1])
        if pm is not None:
            s = pm.deviceIndependentSize()
            p.drawPixmap(QPointF(ir.center().x() - s.width() / 2, ir.center().y() - s.height() / 2), pm)
        p.setFont(design.font("toolbarLabel"))
        p.setPen(design.color("secondaryLabel" if self.isEnabled() else "tertiaryLabel"))
        p.drawText(QRectF(0, self.ICON_H + 2, self.width(), 14), Qt.AlignHCenter | Qt.AlignTop, self.text())
        if self.hasFocus():
            focus_ring(p, ir, ir.height() / 2)


class ToolbarGroup(QWidget):
    """Buttons that belong together, sharing one Liquid Glass capsule behind their icons."""

    def __init__(self, buttons: list[ToolbarButton], parent=None):
        super().__init__(parent)
        self.buttons = buttons
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(0)
        for b in buttons:
            row.addWidget(b)
        self.setFixedHeight(ToolbarButton.HEIGHT)

    def paintEvent(self, e):
        if not self.buttons:
            return
        p = QPainter(self)
        r = QRectF(0, 0, self.width(), ToolbarButton.ICON_H)
        glass_capsule(p, r)


class Toolbar(QWidget):
    """A row of toolbar groups; `add(...)` returns the buttons, `stretch()` adds space.

    On macOS, after `attach_native(window)`, each group also becomes a real NSToolbar item
    group in the window's unified toolbar (Qt can't take clicks in the title bar). The Qt
    buttons stay the model: native clicks call `button.click()`, so signals, checkable state
    and QButtonGroups behave the same on every platform."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(design.SPACING["m"])
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(ToolbarButton.HEIGHT)
        self.window_widget: QWidget | None = None
        self._groups = 0

    def attach_native(self, window: QWidget):
        self.window_widget = window

    def add(self, *specs: tuple, checkable: bool = False) -> list[ToolbarButton]:
        """specs: (symbol, label, glyph[, slot])."""
        buttons = []
        for spec in specs:
            sym, label, glyph = spec[:3]
            b = ToolbarButton(sym, label, glyph, checkable)
            if len(spec) > 3 and spec[3] is not None:
                b.clicked.connect(spec[3])
            buttons.append(b)
        self.row.addWidget(ToolbarGroup(buttons))
        if self.window_widget is not None:
            self._add_native(buttons, checkable)
        return buttons

    def _add_native(self, buttons: list[ToolbarButton], checkable: bool):
        mac = design._mac()
        self._groups += 1
        ident = f"group{self._groups}"
        win = self.window_widget

        def sync():
            for i, b in enumerate(buttons):
                mac.toolbar_set_selected(win, ident, i, b.isChecked())

        def clicked(i: int):
            if 0 <= i < len(buttons) and buttons[i].isEnabled():
                buttons[i].click()
            if checkable:
                sync()
        mode = mac.SELECT_ANY if checkable else mac.MOMENTARY
        if mac.toolbar_add_group(win, ident, [(b.symbol_name, b.text()) for b in buttons], mode, clicked):
            for i, b in enumerate(buttons):
                b.native = (win, ident, i)
                if checkable:
                    b.toggled.connect(lambda *_: sync())
            if checkable:
                sync()

    def stretch(self):
        self.row.addStretch(1)


# ---------------------------------------------------------------- popover + colour well
class Popover(QWidget):
    """A transient panel with Apple's popover shape: rounded, glass, with an arrow pointing at
    the control that opened it. Closes on a click outside. Put content in `body`."""
    ARROW_W, ARROW_H = 20, 9

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.body = QVBoxLayout(self)
        m = design.SPACING["m"]
        self.body.setContentsMargins(m, m + self.ARROW_H, m, m)
        self.body.setSpacing(design.SPACING["s"])
        self.arrow_x = 40.0
        self._vibrant = False

    def path(self) -> QPainterPath:
        r = QRectF(0.5, self.ARROW_H + 0.5, self.width() - 1, self.height() - self.ARROW_H - 1)
        rad = design.RADIUS["popover"]
        body = QPainterPath()
        body.addRoundedRect(r, rad, rad)
        ax = min(max(self.arrow_x, rad + self.ARROW_W / 2), self.width() - rad - self.ARROW_W / 2)
        arrow = QPainterPath()
        arrow.moveTo(ax - self.ARROW_W / 2, self.ARROW_H + 1)
        arrow.cubicTo(ax - 4, self.ARROW_H, ax - 2.5, 0.5, ax, 0.5)
        arrow.cubicTo(ax + 2.5, 0.5, ax + 4, self.ARROW_H, ax + self.ARROW_W / 2, self.ARROW_H + 1)
        arrow.closeSubpath()
        return body.united(arrow)

    def show_for(self, anchor: QWidget):
        self.adjustSize()
        a = anchor.mapToGlobal(QPoint(anchor.width() // 2, anchor.height()))
        scr = (anchor.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        x = min(max(a.x() - self.width() // 2, scr.left() + 8), scr.right() - self.width() - 8)
        y = a.y() + 2
        if y + self.height() > scr.bottom():
            y = max(scr.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 2)
        self.arrow_x = a.x() - x
        self.move(x, y)
        self.show()
        self._apply_material()

    def _apply_material(self):
        if not native():
            return
        from . import platform_mac
        dpr = self.devicePixelRatioF()
        img = QImage(int(self.width() * dpr), int(self.height() * dpr), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(dpr, dpr)
        p.fillPath(self.path(), QColor(0, 0, 0))
        p.end()
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        buf = QByteArray()
        dev = QBuffer(buf)
        dev.open(QIODevice.WriteOnly)
        img.save(dev, "PNG")
        self._vibrant = platform_mac.add_vibrancy(self, "popover", mask=(bytes(buf), self.width(), self.height()),
                                                  follow_active=False)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = self.path()
        if not self._vibrant:
            p.fillPath(path, design.color("glassTintSolid"))
        p.setPen(QPen(design.color("separator", alpha=1.6), design.HAIRLINE))
        p.drawPath(path)


class _Swatch(QAbstractButton):
    def __init__(self, name: str, colors: list[str], parent=None):
        super().__init__(parent)
        self.colors = colors
        self.setCheckable(True)
        self.setFixedSize(24, 24)
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.TabFocus)

    def paintEvent(self, e):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        paint_swatch(p, r, self.colors, r.height() / 2)
        if self.isChecked() or self.hasFocus():
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(design.color("accent" if self.isChecked() else "focusRing"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))


class ColorWell(QPushButton):
    """Pages' colour well: shows the current fill; a click opens a popover of presets plus
    "Other…" (the system colour picker). Emits `picked(colors)`."""
    picked = Signal(list)

    def __init__(self, presets: list[tuple[str, list[str]]], current: list[str] | None = None,
                 columns: int = 8, parent=None):
        super().__init__(parent)
        self.presets = presets
        self.columns = columns
        self.colors = list(current or presets[0][1])
        self.setFixedSize(44, 22)
        self.setAccessibleName("Colour")
        self.setToolTip("Colour")
        self.clicked.connect(self.open_popover)

    def set_colors(self, colors: list[str]):
        self.colors = list(colors)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rad = design.RADIUS["control"]
        glass_capsule(p, r, rad, strong=True)
        paint_swatch(p, r.adjusted(4, 4, -4, -4), self.colors, rad - 3)
        if self.hasFocus():
            focus_ring(p, r, rad)

    def open_popover(self):
        pop = Popover(self.window())
        grid = QGridLayout()
        grid.setSpacing(2)
        grp = QButtonGroup(pop)
        for i, (name, cols) in enumerate(self.presets):
            s = _Swatch(name, cols)
            s.setChecked(cols == self.colors)
            s.clicked.connect(lambda _=False, c=cols, w=pop: (self._choose(c), w.close()))
            grp.addButton(s)
            grid.addWidget(s, i // self.columns, i % self.columns)
        pop.body.addLayout(grid)
        other = QPushButton("Other…")
        other.clicked.connect(lambda: (pop.close(), self._other()))
        pop.body.addWidget(other, 0, Qt.AlignLeft)
        pop.show_for(self)

    def _other(self):
        c = QColorDialog.getColor(QColor(self.colors[0]), self.window(), "Colour")
        if c.isValid():
            self._choose([c.name()])

    def _choose(self, cols: list[str]):
        self.set_colors(cols)
        self.picked.emit(list(cols))
