"""The radial drop menu that pops up under the cursor while you Shift+drag a file.

Rendering: the moment the menu opens we snapshot the screen behind it; a worker thread then
refracts that snapshot through the menu's shape (see liquid.py). Until that's ready (~60 ms)
a cheap frosted version is drawn, so the menu still appears instantly."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QPointF, QRectF, QRunnable, Qt,
                            QThreadPool, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QGuiApplication, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import design, glass


@dataclass
class Item:
    label: str
    glyph: str = ""
    run: Callable[[list[str]], None] | None = None
    key: str = ""
    hint: str = ""


SIZE = 400          # window size (room for shadow + hover pop)
R_OUT = 158         # outer radius of petals
R_IN = 60           # inner radius of petals
HUB_DWELL_MS = 1500  # resting in the hub this long shows the next ring; long enough to read it
R_HUB = 50          # hub radius
GAP_DEG = 3.2
CORNER = design.RADIUS["petal"]
SYMBOL_PT = 20      # SF Symbol size on tool petals (scaled by the painter on hover, never re-rendered)


def sector_path(c: QPointF, r0: float, r1: float, a0: float, a1: float) -> QPainterPath:
    """Annular sector between angles a0..a1 (degrees, Qt convention: 0 = 3 o'clock, CCW)."""
    path = QPainterPath()
    outer = QRectF(c.x() - r1, c.y() - r1, 2 * r1, 2 * r1)
    inner = QRectF(c.x() - r0, c.y() - r0, 2 * r0, 2 * r0)
    path.arcMoveTo(outer, a0)
    path.arcTo(outer, a0, a1 - a0)
    path.arcTo(inner, a1, -(a1 - a0))
    path.closeSubpath()
    return path


_ROUNDED: dict[tuple, QPainterPath] = {}


def rounded_sector_path(c: QPointF, r0: float, r1: float, a0: float, a1: float, rr: float) -> QPainterPath:
    """`sector_path` with rounded corners, matching the liquid render's petals (cached)."""
    key = (c.x(), c.y(), r0, r1, round(a0, 3), round(a1, 3), rr)
    path = _ROUNDED.get(key)
    if path is None:
        from PySide6.QtGui import QPainterPathStroker
        inset = math.degrees(rr / ((r0 + r1) / 2))      # side offset by ~rr at mid radius
        core = sector_path(c, r0 + rr, r1 - rr, a0 + inset, a1 - inset)
        st = QPainterPathStroker()
        st.setWidth(2 * rr)
        st.setJoinStyle(Qt.RoundJoin)
        path = _ROUNDED[key] = core.united(st.createStroke(core)).simplified()
    return path


def angles(n: int, i: int) -> tuple[float, float]:
    span = 360.0 / n
    start = 90 + span / 2 - (i + 1) * span   # first petal centred at 12 o'clock, clockwise
    return start + GAP_DEG / 2, start + span - GAP_DEG / 2


def polar(c: QPointF, r: float, deg: float) -> QPointF:
    """The point `r` from `c` at angle `deg` (Qt convention, y grows downwards)."""
    rad = math.radians(deg)
    return QPointF(c.x() + r * math.cos(rad), c.y() - r * math.sin(rad))


def step(index: int, count: int, delta: int) -> int:
    """Keyboard: the petal `delta` places clockwise from `index`, wrapping round. With nothing
    highlighted yet (index < 0), Right starts at the top petal and Left at the last one."""
    if count <= 0:
        return -1
    if index < 0:
        return 0 if delta > 0 else count - 1
    return (index + delta) % count


# keyboard control: Qt keys (menu has focus) map to the names the watcher polls during a drag
NAV_QT_KEYS = {Qt.Key_Left: "left", Qt.Key_Right: "right", Qt.Key_Up: "up", Qt.Key_Down: "down",
               Qt.Key_Return: "enter", Qt.Key_Enter: "enter", Qt.Key_Escape: "escape", Qt.Key_Tab: "tab"}
POINTER_SLOP = 3    # px the pointer must move to take the highlight back from the keyboard
RELEASE_HINT = "Release to finish"


def modifier_name(key: int) -> str:
    """Settings' key name for a modifier key press. Qt on macOS reports Command as Key_Control
    and Control as Key_Meta."""
    mac = sys.platform == "darwin"
    return {Qt.Key_Shift: "shift", Qt.Key_Alt: "alt", Qt.Key_Control: "cmd" if mac else "ctrl",
            Qt.Key_Meta: "ctrl" if mac else "cmd"}.get(key, "")


# ---------------------------------------------------------------- background render
class _RenderSig(QObject):
    done = Signal(int, int, QImage, bool)   # serial, petal count, image, is_light


class _RenderJob(QRunnable):
    def __init__(self, serial: int, n: int, backdrop):
        super().__init__()
        self.serial, self.n, self.backdrop = serial, n, backdrop
        self.sig = _RenderSig()

    def run(self):
        try:
            from . import liquid
            c = SIZE / 2
            fields = [liquid.sdf_circle(SIZE, SIZE, c, c, R_HUB)]
            for i in range(self.n):
                a0, a1 = angles(self.n, i)
                fields.append(liquid.sdf_ring_sector(SIZE, SIZE, c, c, R_IN, R_OUT, a0, a1, CORNER))
            sdf = liquid.union(*fields)
            img, light = liquid.render(self.backdrop, sdf, liquid.GlassStyle())
            sh = liquid.shadow(sdf, spread=26, opacity=0.42 if light else 0.30, offset=(0, 8))
            sh.alpha_composite(img)
            try:
                self.sig.done.emit(self.serial, self.n, glass.qimage_from_pil(sh), light)
            except RuntimeError:
                pass
        except Exception:  # pragma: no cover - fall back to the painted glass
            import traceback
            traceback.print_exc()


class RadialMenu(QWidget):
    activated = Signal(object, list)     # Item, paths
    dismissed = Signal()

    def __init__(self, build_rings: Callable[[list[str]], dict[str, list[Item]]],
                 describe: Callable[[list[str]], str]):
        super().__init__(None)
        self.build_rings = build_rings
        self.describe = describe
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(SIZE, SIZE)

        self.paths: list[str] = []
        self.rings: dict[str, list[Item]] = {}
        self.ring_names: list[str] = []
        self.ring_idx = 0
        self.wanted_ring = ""    # ring asked for by the key that opened the menu (shown once loaded)
        self.ring_keys: dict[str, str] = {}   # ring name -> key glyph for the hub hint, e.g. "⌘"
        self.wheel_keys: dict[str, str] = {}  # ring name -> Settings key name, e.g. "cmd"
        self.hover = -1          # petal index, -2 = hub, -1 = none
        self.kbd = False         # the highlight came from the keyboard, not the pointer
        self.last_pointer = QPointF(SIZE / 2, SIZE / 2)
        self.committed: Item | None = None   # Enter ran this mid-drag; waiting for the drag to end
        self._return_to = None   # macOS: app to hand focus back to when a keyboard menu is dismissed
        self.cursor_pos = QPointF(SIZE / 2, SIZE / 2)
        self.hub_text = ""
        self.backdrop: QPixmap | None = None     # blurred, for the instant fallback
        self._raw_backdrop = None                 # PIL image for the liquid renderer
        self.liquid: dict[int, QImage] = {}       # petal count -> rendered glass
        self.is_light = False
        self._serial = 0
        self.got_drag = False
        self.hub_armed = False
        self.click_mode = False  # opened from tray/menu instead of a drag
        self._t = 1.0            # pop-in animation 0..1
        self._swap = 1.0         # ring-swap animation 0..1
        self.spring: list[float] = []   # per-petal hover amount 0..1 (eased every frame)
        self.hub_spring = 0.0

        self.anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=300,
                                      easingCurve=design.spring_curve(damping=0.72, response=0.42))
        self.anim.valueChanged.connect(self._set_t)
        self.swap_anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=design.DURATION["standard"],
                                           easingCurve=QEasingCurve.OutCubic)
        self.swap_anim.valueChanged.connect(self._set_swap)
        self.hub_timer = QTimer(self, singleShot=True, interval=HUB_DWELL_MS, timeout=self.next_ring)
        self.frame = QTimer(self, interval=16, timeout=self._tick)   # 60 fps springs
        # load SF Symbols and fonts while idle after start-up, so the first menu opens instantly
        QTimer.singleShot(0, self._warm)

    def _warm(self):
        """Render every petal symbol and label font once (the caches keep them)."""
        from PySide6.QtGui import QPixmap as _Pixmap
        colors = [design.color("label", dark=True), design.color("label", dark=False), design.color("onAccent")]
        for c in colors:
            c.setAlpha(255)
            for label, sym in design.SYMBOLS.items():
                design.symbol(sym, SYMBOL_PT, c, fallback="•")
        pm = _Pixmap(8, 8)
        p = QPainter(pm)
        for role in ("hubTop", "hubSub", "tool", "format"):
            p.setFont(_font(role))
            p.drawText(0, 8, "PNG Aa")
        p.end()

    # ------------------------------------------------------------ show / hide
    def popup(self, pos: QPoint | None = None, paths: list[str] | None = None, ring: str = "",
              dragged: list[str] | None = None):
        """Open under `pos`. `ring` picks the starting ring (the wheel whose key was pressed);
        when the files don't have that ring, the first ring they do have is shown.
        `paths` opens a click menu (tray, Services); `dragged` are files already known to be on
        the drag (macOS drag pasteboard), so the rings and keyboard work before the drag reaches
        the window."""
        pos = pos or QCursor.pos()
        self.wanted_ring = ring
        tl = pos - QPoint(SIZE // 2, SIZE // 2)
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        g = scr.geometry()
        tl.setX(min(max(tl.x(), g.left()), g.right() - SIZE))
        tl.setY(min(max(tl.y(), g.top()), g.bottom() - SIZE))
        # snapshot what's underneath *before* we appear
        self._serial += 1
        self.liquid = {}
        try:
            if sys.platform == "darwin":
                # macOS captures "below the drag window": the drag picture never gets in
                raw = glass.grab_screen(tl.x(), tl.y(), SIZE, SIZE)
                if raw is None:
                    raise RuntimeError("capture failed")
                from . import platform_mac
                print("menu: mac windows at cursor:", "; ".join(platform_mac.LAST_SWEEP), flush=True)
                if self.got_drag or paths is None:
                    # belt and braces: Finder's drag picture (icon + name label) sits on the
                    # cursor; erase that area — it ends up under the hub, blurred anyway
                    cx, cy = pos.x() - tl.x(), pos.y() - tl.y()
                    raw = glass.inpaint_rects(raw, [(cx - 70, cy - 64, cx + 70, cy + 64)])
                shot = QPixmap.fromImage(glass.qimage_from_pil(raw))
            else:
                with glass.hidden_drag_images() as hid:
                    shot = scr.grabWindow(0, tl.x() - g.left(), tl.y() - g.top(), SIZE, SIZE)
                if hid.hwnds:
                    print(f"menu: hid {len(hid.hwnds)} drag image(s) during capture: {'; '.join(hid.info)}", flush=True)
                elif hid.info:
                    print(f"menu: drag-image sweep: {'; '.join(hid.info)}", flush=True)
                raw = glass.pil_from_qimage(
                    shot.toImage().scaled(SIZE, SIZE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
                if sys.platform.startswith("linux") and paths is None:
                    # X11 drag icons (Thunar/GTK) sit on the cursor: erase that patch
                    cx, cy = pos.x() - tl.x(), pos.y() - tl.y()
                    hid.rects = [(pos.x() - 60, pos.y() - 60, pos.x() + 60, pos.y() + 60)]
                if hid.rects:
                    # belt and braces: erase where the drag picture was, in case it was captured anyway
                    k = SIZE / max(1, shot.width())
                    local = [(int((x0 - tl.x()) * k), int((y0 - tl.y()) * k), int((x1 - tl.x()) * k), int((y1 - tl.y()) * k))
                             for (x0, y0, x1, y1) in hid.rects]
                    raw = glass.inpaint_rects(raw, local)
                    shot = QPixmap.fromImage(glass.qimage_from_pil(raw))
            self._raw_backdrop = raw
            self.backdrop = glass.blur_pixmap(shot, 24)
        except Exception:
            self.backdrop = None
            self._raw_backdrop = None
        self.move(tl)
        self.hover = -1
        self.kbd = False
        self.committed = None
        self.last_pointer = QPointF(pos - tl)
        self.got_drag = False
        self.hub_armed = False  # drags start on the hub; don't flip until they leave it
        self.click_mode = paths is not None
        self._set_focusable(self.click_mode)
        if paths is not None:
            self._load(paths)
        elif dragged:
            self.got_drag = True
            self._load(dragged)
        else:
            self.paths, self.rings, self.ring_names = [], {}, []
            self.hub_text = "Drop here"
            for n in (4, 7):  # most common rings: render speculatively
                self._render(n)
        self._t = 0.0
        self._still = design.reduce_motion()   # Reduce motion: fade in, no scale/spring
        self.anim.setDuration(design.DURATION["fast"] if self._still else 300)
        self._shape()
        self.show()
        self.raise_()
        self._announce()
        self.anim.start()
        self.frame.start()
        if self.click_mode:
            self._take_focus()
            # behave like a popup: a click anywhere outside dismisses it
            QTimer.singleShot(0, self.grabMouse)

    def close_menu(self):
        if self.isVisible():
            self.hide()
            self.frame.stop()
            self.hub_timer.stop()
            self.dismissed.emit()
            if self.click_mode:
                self._give_back_focus()

    def _set_focusable(self, on: bool):
        """Drag menus never take focus (the drag owns input; keys come from the watcher). Click
        menus (tray, Services shortcut) take it, so the arrow keys reach keyPressEvent."""
        if bool(self.windowFlags() & Qt.WindowDoesNotAcceptFocus) == on:
            self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, not on)
        self.setAttribute(Qt.WA_ShowWithoutActivating, not on)

    def _take_focus(self):
        if sys.platform == "darwin":
            from . import platform_mac
            self._return_to = platform_mac.frontmost_app()
            platform_mac.activate()   # a menu-bar app isn't active: its window can't get keys otherwise
        self.activateWindow()
        self.setFocus(Qt.PopupFocusReason)

    def _give_back_focus(self):
        """Dismissed with Escape or a click outside: Finder (or whoever had it) gets focus back."""
        app, self._return_to = self._return_to, None
        if app is not None and sys.platform == "darwin":
            from . import platform_mac
            platform_mac.reactivate(app)

    def hideEvent(self, e):
        self.frame.stop()
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        super().hideEvent(e)

    def _render(self, n: int):
        if n <= 0 or n in self.liquid or self._raw_backdrop is None:
            return
        self.liquid[n] = QImage()  # placeholder: in flight
        job = _RenderJob(self._serial, n, self._raw_backdrop)
        from .util import keep, release
        job.sig.done.connect(lambda *a, j=job: (release(j), self._rendered(*a)))
        QThreadPool.globalInstance().start(keep(job))

    def _rendered(self, serial: int, n: int, img: QImage, light: bool):
        if serial == self._serial:
            self.liquid[n] = img
            self.is_light = light
            self.update()

    def _load(self, paths: list[str]):
        self.paths = paths
        self.rings = {k: v for k, v in self.build_rings(paths).items() if v}
        self.ring_names = list(self.rings)
        self.ring_idx = self.ring_names.index(self.wanted_ring) if self.wanted_ring in self.ring_names else 0
        self.hub_text = self.describe(paths)
        for items in self.rings.values():
            self._render(len(items))
        self.spring = [0.0] * len(self.items)
        self._shape()
        self._announce()
        self.update()

    def _announce(self):
        """VoiceOver: name the menu, the current ring and the highlighted item (pointer or keys)."""
        items = self.items
        ring = self.ring_names[self.ring_idx] if self.ring_names else ""
        if self.committed is not None or 0 <= self.hover < len(items):
            name = ", ".join(t for t in self.hub_lines() if t)
        else:
            name = f"DragShift {ring} menu" if ring else "DragShift menu"
        self.setAccessibleName(name)
        self.setAccessibleDescription(", ".join(i.label for i in items))
        try:
            from PySide6.QtGui import QAccessible, QAccessibleEvent
            if QAccessible.isActive():
                QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.NameChanged))
        except Exception:
            pass

    def _shape(self):
        """No compositor (XFCE etc.): cut the window to the menu's outline."""
        if not glass.no_compositor():
            return
        c = QPointF(SIZE / 2, SIZE / 2)
        path = QPainterPath()
        path.addEllipse(c, R_HUB + 2, R_HUB + 2)
        n = len(self.items) or 0
        if n:
            for i in range(n):
                a0, a1 = angles(n, i)
                path = path.united(sector_path(c, R_IN - 2, R_OUT + 14, a0 - 1, a1 + 1))
        else:
            outer = QPainterPath()
            outer.addEllipse(c, R_OUT + 14, R_OUT + 14)
            path = path.united(outer)       # united, not added: odd-even fill would punch a hole
        glass.apply_shape(self, path)

    def next_ring(self):
        if len(self.ring_names) > 1:
            self._go_to((self.ring_idx + 1) % len(self.ring_names))

    def show_ring(self, name: str):
        """Switch straight to the named ring. Before the files arrive, remember it instead."""
        if not self.ring_names:
            self.wanted_ring = name
        elif name in self.ring_names and self.ring_names.index(name) != self.ring_idx:
            self._go_to(self.ring_names.index(name))

    def ring_key(self, name: str):
        """A wheel's key was pressed while open: show that wheel, or the next ring when it's
        already showing (tapping the same key again cycles, as with one key before)."""
        if self.ring_names and self.ring_names[self.ring_idx] == name:
            self.next_ring()
        else:
            self.show_ring(name)

    # ------------------------------------------------------------ keyboard
    def nav(self, key: str):
        """Keyboard control, from keyPressEvent (click menus have focus) or the watcher (during a
        drag the menu never gets focus). Left/Right move round the wheel, Up/Down and Tab switch
        wheels keeping the position, Enter picks, Escape closes."""
        if key == "escape":
            self.close_menu()
        elif self.committed is not None:
            return    # Enter already ran an item: only waiting for the drag to end
        elif key in ("left", "right"):
            self._key_select(step(self.hover, len(self.items), 1 if key == "right" else -1))
        elif key in ("up", "down", "tab"):
            self._key_ring(-1 if key == "up" else 1)
        elif key == "enter":
            self._enter()

    def _key_select(self, idx: int):
        """Highlight a petal from the keyboard: the same accent petal as a hover, with the light
        gathered at the petal's centre instead of under the pointer."""
        if idx < 0:
            return
        self.kbd = True
        n = len(self.items)
        a0, a1 = angles(n, idx)
        self._set_hover(idx, polar(QPointF(SIZE / 2, SIZE / 2), (R_IN + R_OUT) / 2, (a0 + a1) / 2))

    def _key_ring(self, delta: int):
        if len(self.ring_names) < 2:
            return
        keep = self.hover
        self._go_to((self.ring_idx + delta) % len(self.ring_names))
        if keep >= 0:
            self._key_select(min(keep, len(self.items) - 1))

    def _enter(self):
        if self.hover == -2:
            self.next_ring()      # same as clicking the hub
        elif not 0 <= self.hover < len(self.items):
            return
        elif self.click_mode:
            self._activate(self.hover)
        else:
            # mid-drag: the files are known (drag pasteboard or the drag entering us), so run now.
            # The menu stays up as a harmless place to let go, so the drop can't land on a folder
            # underneath and move the file; the release (or Escape) closes it.
            item, paths = self.items[self.hover], list(self.paths)
            self.committed = item
            self.hub_timer.stop()
            self._announce()
            self.update()
            QTimer.singleShot(0, lambda: self.activated.emit(item, paths))

    def _activate(self, idx: int) -> bool:
        """Run petal `idx` (drop, click or Enter) after closing; False when it isn't a petal."""
        items = self.items
        if not 0 <= idx < len(items):
            return False
        item, paths = items[idx], list(self.paths)
        self.hide()
        # return to the drag loop / event loop immediately; do the work after
        QTimer.singleShot(0, lambda: self.activated.emit(item, paths))
        return True

    def _wheel_for(self, key_name: str) -> str:
        return next((wheel for wheel, key in self.wheel_keys.items() if key == key_name), "") if key_name else ""

    def _go_to(self, idx: int):
        """Animated switch to ring `idx` (the same swap for keys, hub dwell and clicks)."""
        self.ring_idx = idx
        if self.hover == -2:
            self.hub_timer.start()  # still resting in the hub: next flip only after another full dwell
        else:
            self.hover = -1
        self.spring = [0.0] * len(self.items)
        self._swap = 0.0
        self.swap_anim.start()
        self._shape()
        self._announce()
        self.update()

    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def _set_swap(self, v):
        self._swap = float(v)
        self.update()

    def _tick(self):
        moved = False
        for i in range(len(self.spring)):
            target = 1.0 if i == self.hover else 0.0
            v = self.spring[i]
            nv = v + (target - v) * 0.32
            if abs(nv - target) < 0.003:
                nv = target
            if nv != v:
                self.spring[i] = nv
                moved = True
        ht = 1.0 if self.hover == -2 else 0.0
        nh = self.hub_spring + (ht - self.hub_spring) * 0.3
        if abs(nh - ht) < 0.003:
            nh = ht
        if nh != self.hub_spring:
            self.hub_spring = nh
            moved = True
        if moved:
            self.update()

    # ------------------------------------------------------------ geometry
    @property
    def items(self) -> list[Item]:
        if not self.ring_names:
            return []
        return self.rings[self.ring_names[self.ring_idx]]

    def _angles(self, n: int, i: int) -> tuple[float, float]:
        return angles(n, i)

    def hit(self, pos: QPointF) -> int:
        c = QPointF(SIZE / 2, SIZE / 2)
        dx, dy = pos.x() - c.x(), c.y() - pos.y()
        d = math.hypot(dx, dy)
        if d <= R_HUB + 4:
            return -2
        if d > R_OUT + 30 or not self.items:
            return -1
        ang = math.degrees(math.atan2(dy, dx)) % 360
        n = len(self.items)
        rel = (90 + 180.0 / n - ang) % 360
        return int(rel // (360.0 / n)) % n

    def _set_hover(self, h: int, pos: QPointF | None = None):
        if pos is not None:
            self.cursor_pos = QPointF(pos)
        if h != self.hover:
            self.hover = h
            if h >= 0:
                self.hub_armed = True  # visited a petal: hovering the hub may now flip rings
            self._announce()
            if h == -2 and self.hub_armed and len(self.ring_names) > 1:
                self.hub_timer.start()
            else:
                self.hub_timer.stop()
        self.update()

    # ------------------------------------------------------------ drag & drop
    @staticmethod
    def _local_paths(md) -> list[str]:
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        paths = self._local_paths(e.mimeData())
        if not paths:
            e.ignore()
            return
        self.got_drag = True
        if paths != self.paths:
            self._load(paths)
        e.setDropAction(Qt.CopyAction)  # never let a Shift(=move) drop delete the source
        e.accept()

    def dragMoveEvent(self, e):
        self._pointer(e.position())
        e.setDropAction(Qt.CopyAction)
        e.accept()

    def dragLeaveEvent(self, e):
        self._pointer_left()

    def dropEvent(self, e):
        if self.committed is not None:
            # Enter already ran the item: swallow the drop (as a copy, so nothing moves)
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self.hide()
            return
        idx = self.target(e.position())
        if 0 <= idx < len(self.items):
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self._activate(idx)
        else:
            e.ignore()
            self.close_menu()

    def _moved(self, pos: QPointF) -> bool:
        return (pos - self.last_pointer).manhattanLength() >= POINTER_SLOP

    def target(self, pos: QPointF) -> int:
        """The petal a drop at `pos` runs: the keyboard's pick while the pointer stays put (so
        arrowing to a petal and letting go works like Enter), otherwise the one under it."""
        return self.hover if self.kbd and not self._moved(pos) else self.hit(pos)

    def _pointer(self, pos: QPointF):
        """The pointer (or a drag) is over the menu. It takes the highlight back from the
        keyboard only once it really moves: macOS repeats drag-move events while it's still."""
        if self.committed is not None or (self.kbd and not self._moved(pos)):
            return
        self.kbd = False
        self.last_pointer = QPointF(pos)
        self._set_hover(self.hit(pos), pos)

    def _pointer_left(self):
        if self.committed is None and not self.kbd:
            self._set_hover(-1)

    # ------------------------------------------------------------ click mode
    def mouseMoveEvent(self, e):
        if self.click_mode:
            self._pointer(e.position())

    def mouseReleaseEvent(self, e):
        if not self.click_mode:
            return
        if e.button() == Qt.RightButton:
            self.close_menu()
            return
        idx = self.hit(e.position())
        if idx == -2:
            self.next_ring()
        elif not self._activate(idx):
            self.close_menu()

    def keyPressEvent(self, e):
        """Click menus have focus: the same keys as the watcher polls during a drag, plus a
        wheel's key (Shift, Command, Control...) jumps to that wheel."""
        key = NAV_QT_KEYS.get(e.key())
        wheel = self._wheel_for(modifier_name(e.key()))
        if key:
            self.nav(key)
        elif wheel:
            self.ring_key(wheel)
        elif e.key() == Qt.Key_Shift:
            self.next_ring()
        else:
            super().keyPressEvent(e)

    def focusNextPrevChild(self, forward: bool) -> bool:
        return False   # Tab switches wheels; don't let Qt use it to move focus

    def leaveEvent(self, e):
        if self.click_mode:
            self._pointer_left()

    # ------------------------------------------------------------ painting
    def _text_colors(self) -> tuple[QColor, QColor, QColor]:
        """Label, secondary label and a soft text shadow, for the glass's own brightness
        (Liquid Glass adapts to what's behind it, not to the system appearance)."""
        dark = not self.is_light
        shadow = QColor(255, 255, 255, 90) if self.is_light else QColor(0, 0, 0, 90)
        fg = design.color("label", dark=dark)
        fg.setAlpha(255)   # full-strength labels on glass: the backdrop varies too much for 85%
        return fg, design.color("secondaryLabel", dark=dark, alpha=1.25), shadow

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        c = QPointF(SIZE / 2, SIZE / 2)
        t = self._t
        if getattr(self, "_still", False):
            p.setOpacity(min(1.0, max(0.0, t)))
        else:
            p.setOpacity(min(1.0, max(0.0, t * 1.8)))
            p.translate(c)
            s = 0.86 + 0.14 * t
            p.scale(s, s)
            p.translate(-c)

        items = self.items
        n = len(items)
        img = self.liquid.get(n) if n else (self.liquid.get(4) or self.liquid.get(7))
        have_liquid = img is not None and not img.isNull()

        if have_liquid:
            p.drawImage(0, 0, img)
        else:
            self._paint_fallback(p, c, n)

        fg, fg_dim, shadow_col = self._text_colors()
        rot = (1 - self._swap) * -22
        for i, it in enumerate(items):
            a0, a1 = angles(n, i)
            h = self.spring[i] if i < len(self.spring) else 0.0
            if h > 0.001:
                self._paint_hover(p, c, a0, a1, h, img if have_liquid else None)
            self._petal_label(p, c, it, (a0 + a1) / 2 + rot, (R_IN + R_OUT) / 2 + h * 4, h,
                              fg, shadow_col)

        self._paint_hub(p, c, fg, fg_dim, shadow_col)

    def _paint_fallback(self, p: QPainter, c: QPointF, n: int):
        tint = design.color("glassTintSolid" if design.reduce_transparency() else "glassTint", dark=True)
        tint.setAlpha(min(tint.alpha(), 120) if not design.reduce_transparency() else 240)
        hub = QPainterPath()
        hub.addEllipse(c, R_HUB, R_HUB)
        glass.paint_glass(p, hub, self.backdrop, QPointF(0, 0), tint=tint, rainbow_alpha=0, highlight=1.2)
        for i in range(n):
            a0, a1 = angles(n, i)
            glass.paint_glass(p, sector_path(c, R_IN, R_OUT, a0, a1), self.backdrop, QPointF(0, 0),
                              tint=tint, rainbow_alpha=0)

    def _paint_hover(self, p: QPainter, c: QPointF, a0: float, a1: float, h: float, img: QImage | None):
        """Selected petal: the accent colour, like a selected menu item, with light gathering
        under the pointer and a brighter rim; it lifts towards the pointer."""
        mid = math.radians((a0 + a1) / 2)
        pc = polar(c, (R_IN + R_OUT) / 2, (a0 + a1) / 2)
        lift = 0.0 if getattr(self, "_still", False) else h
        k = 1 + 0.06 * lift
        push = 6 * lift
        off = QPointF(push * math.cos(mid), -push * math.sin(mid))
        path = rounded_sector_path(c, R_IN, R_OUT, a0, a1, CORNER)
        p.save()
        p.translate(pc + off)
        p.scale(k, k)
        p.translate(-pc)
        if img is not None:
            p.save()
            p.setClipPath(path)
            p.drawImage(0, 0, img)
            p.restore()
        p.fillPath(path, design.color("accent", alpha=0.82 * h))
        glow = QRadialGradient(self.cursor_pos - off, 110)
        glow.setColorAt(0.0, QColor(255, 255, 255, int(70 * h)))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, glow)
        p.setPen(QPen(QColor(255, 255, 255, int(150 * h)), 1.2))
        p.drawPath(path)
        p.restore()

    def hub_lines(self) -> tuple[str, str]:
        """Hub text: the hovered item and its hint, or the files and the next ring with its
        key (e.g. "⌘ Tools"). After Enter mid-drag: the item and a nudge to let go."""
        if self.committed is not None:
            return self.committed.label, RELEASE_HINT
        items = self.items
        if 0 <= self.hover < len(items):
            return items[self.hover].label, items[self.hover].hint or ""
        if len(self.ring_names) > 1:
            other = self.ring_names[(self.ring_idx + 1) % len(self.ring_names)]
            return self.hub_text, f"{self.ring_keys.get(other, '')} {other}".strip()
        return self.hub_text, self.ring_names[self.ring_idx] if self.ring_names else ""

    def _paint_hub(self, p: QPainter, c: QPointF, fg: QColor, fg_dim: QColor, shadow_col: QColor):
        hs = self.hub_spring
        if hs > 0.001:
            g = QRadialGradient(c, R_HUB)
            g.setColorAt(0, QColor(255, 255, 255, int(60 * hs)))
            g.setColorAt(1, QColor(255, 255, 255, int(18 * hs)))
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(c, R_HUB, R_HUB)

        top, sub = self.hub_lines()
        p.setFont(_font("hubTop"))
        rect_top = QRectF(c.x() - R_HUB + 4, c.y() - 20, 2 * R_HUB - 8, 18)
        self._text(p, rect_top, p.fontMetrics().elidedText(top, Qt.ElideRight, int(rect_top.width())), fg, shadow_col)
        p.setFont(_font("hubSub"))
        self._text(p, QRectF(c.x() - R_HUB + 6, c.y() + 1, 2 * R_HUB - 12, 28), sub, fg_dim, None,
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap)

    @staticmethod
    def _text(p: QPainter, r: QRectF, text: str, col: QColor, shadow: QColor | None,
              flags=Qt.AlignCenter):
        if shadow is not None:
            p.setPen(shadow)
            p.drawText(r.translated(0, 0.5), flags, text)
        p.setPen(col)
        p.drawText(r, flags, text)

    def _petal_label(self, p: QPainter, c: QPointF, it: Item, ang: float, r: float,
                     h: float, fg: QColor, shadow_col: QColor):
        push = 0.0 if getattr(self, "_still", False) else 6 * h
        pt = polar(c, r + push, ang)
        p.save()
        p.setOpacity(p.opacity() * (0.25 + 0.75 * self._swap))
        selected = h > 0.5
        col = design.color("onAccent") if selected else fg     # text on the accent fill
        shadow = None if selected else shadow_col
        sym = design.SYMBOLS.get(it.label, "") if it.glyph else ""
        if it.glyph:
            # tool petal: SF Symbol above a small label (Pages' toolbar items)
            pm = design.symbol(sym, SYMBOL_PT, col, fallback=it.glyph)
            if pm is not None:
                sz = pm.deviceIndependentSize()
                k = 1 + 0.12 * h
                p.save()
                p.translate(pt.x(), pt.y() - 11)
                p.scale(k, k)
                p.drawPixmap(QPointF(-sz.width() / 2, -sz.height() / 2), pm)
                p.restore()
            p.setFont(_font("tool"))
            self._text(p, QRectF(pt.x() - 44, pt.y() + 4, 88, 16), it.label, col, shadow)
        else:
            p.setFont(_font("format"))
            k = 1 + 0.1 * h
            p.translate(pt)
            p.scale(k, k)
            self._text(p, QRectF(-44, -12, 88, 24), it.label, col, shadow)
        p.restore()


_FONTS: dict[str, object] = {}


def _font(role: str):
    """Fonts used every frame, built once on first paint (needs a running app)."""
    f = _FONTS.get(role)
    if f is None:
        f = _FONTS[role] = design.font(*{"hubTop": ("headline",), "hubSub": ("caption1", 500),
                                         "tool": ("subheadline", 600), "format": ("title3", 600)}[role])
    return f
