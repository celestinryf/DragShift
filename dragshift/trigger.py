"""Watches for "left button held + mouse moved + a wheel key pressed" anywhere on screen,
which is what pressing Shift (or Command, Control...) while dragging a file looks like.
Each wheel has its own key (Settings); pressing another wheel's key while the menu is open
switches to that wheel. A key only counts once it goes down after the mouse button: a key that
was already held when the button went down (a normal Command-drag in Finder, Shift-click to
extend a selection) is ignored until it's released and pressed again. Polling global input state is simple, needs no admin rights and
can't stall the system the way a broken hook can.
Windows: GetAsyncKeyState. macOS: NSEvent.pressedMouseButtons / modifierFlags (no
Accessibility or Input Monitoring permission required).

While a drag owns the mouse, the menu never gets keyboard focus, so the arrow keys, Enter and
Tab that drive the wheel are polled here too (with key repeat) and handed over as `nav_key`."""
from __future__ import annotations

import ctypes
import sys
import time

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QCursor

VK_LBUTTON, VK_SHIFT, VK_ESCAPE = 0x01, 0x10, 0x1B
# virtual keys per key name, any of which counts. Shift, Ctrl and Alt have one code for either
# side; the Windows key ("cmd") has two: left 0x5B and right 0x5C
KEY_VK = {"shift": (VK_SHIFT,), "ctrl": (0x11,), "alt": (0x12,), "cmd": (0x5B, 0x5C)}
NAV_KEYS = ("left", "right", "up", "down", "enter", "tab")   # polled while the drag menu is open
NAV_VK = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "enter": 0x0D, "tab": 0x09}
REPEATING = frozenset({"left", "right", "up", "down"})
EPSILON = 1e-6        # s; 0.4 + 0.08 isn't exactly 0.48 in floating point
DRAG_THRESHOLD = 10   # px the mouse must travel with the button held
NO_DRAG_TIMEOUT = 50  # ticks (≈ 0.75 s) to wait for a file drag to enter before hiding


class KeyRepeat:
    """Turns polled "key is down" states into presses with the system's key repeat: one press
    when the key goes down, then after `delay` seconds one every `interval` while it's held.
    Keys outside `repeating` (Enter, Tab) fire once per press."""

    def __init__(self, delay: float = 0.4, interval: float = 0.08, repeating=REPEATING):
        self.delay, self.interval, self.repeating = delay, interval, repeating
        self._due: dict[str, float] = {}   # held key -> time of its next repeat

    def reset(self):
        self._due.clear()

    def feed(self, held: set[str], now: float) -> list[str]:
        fired = []
        for key in NAV_KEYS:
            if key not in held:
                self._due.pop(key, None)
            elif key not in self._due:
                fired.append(key)
                self._due[key] = now + self.delay if key in self.repeating else float("inf")
            elif now + EPSILON >= self._due[key]:
                fired.append(key)
                due = self._due[key] + self.interval
                # a late tick fires once, it doesn't burst to catch up
                self._due[key] = due if due > now + EPSILON else now + self.interval
        return fired


def _repeat_for(platform) -> KeyRepeat:
    if platform is not None and hasattr(platform, "key_repeat"):
        return KeyRepeat(*platform.key_repeat())
    return KeyRepeat()


class DragShiftWatcher(QObject):
    show_menu = Signal(QPoint, str)   # where, which wheel
    switch_ring = Signal(str)         # a wheel's key went down while the menu is open
    nav_key = Signal(str)             # an arrow, Enter or Tab press (or repeat) while the menu is open
    hide_menu = Signal()

    def __init__(self, menu_visible, menu_got_drag, parent=None):
        super().__init__(parent)
        self.menu_visible = menu_visible
        self.menu_got_drag = menu_got_drag
        self.enabled = True
        self.keys: dict[str, str] = {"Convert": "shift"}   # wheel -> key name, in ring order
        self._down_at: QPoint | None = None
        self._held: set[str] = set()      # wheels whose key was down on the previous tick
        self._stale: set[str] = set()     # wheels whose key was already down at mouse-down
        self._fired = False
        self._idle_ticks = 0
        self._shown_at = QPoint()
        self._user32 = ctypes.windll.user32 if sys.platform == "win32" else None
        self._mac = None
        if sys.platform == "darwin":
            from . import platform_mac
            self._mac = platform_mac if platform_mac.OK else None
        elif sys.platform.startswith("linux"):
            from . import platform_linux
            # same interface as platform_mac: left_button_down / key_down / escape_down
            self._mac = platform_linux if platform_linux.available() else None
        self.repeat = _repeat_for(self._mac)
        self.clock = time.monotonic
        self._drag_count = -1   # drag pasteboard change count at mouse-down (macOS)
        self.timer = QTimer(self, interval=15, timeout=self._tick)

    def set_keys(self, keys: dict[str, str]):
        self.keys = dict(keys)

    def start(self):
        if self._user32 is not None or self._mac is not None:
            self.timer.start()

    def _down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _key_down(self, name: str) -> bool:
        if self._mac is not None:
            return self._mac.key_down(name)
        return any(self._down(vk) for vk in KEY_VK.get(name, (VK_SHIFT,)))

    def _state(self) -> tuple[bool, set[str], bool]:
        """(left button, wheels whose key is down, escape)"""
        held = {wheel for wheel, key in self.keys.items() if self._key_down(key)}
        if self._mac is not None:
            return self._mac.left_button_down(), held, self._mac.escape_down()
        return self._down(VK_LBUTTON), held, self._down(VK_ESCAPE)

    def _nav_state(self) -> set[str]:
        """Navigation keys held right now."""
        if self._mac is not None:
            return {name for name in NAV_KEYS if self._mac.nav_down(name)}
        if self._user32 is not None:
            return {name for name, vk in NAV_VK.items() if self._down(vk)}
        return set()

    def drag_paths(self) -> list[str]:
        """Files being dragged right now, when the platform lets us see them before the drag
        reaches our window (macOS drag pasteboard). Empty when unknown or left over from an
        earlier drag."""
        plat = self._mac
        if plat is None or not hasattr(plat, "drag_paths") or self._drag_count < 0:
            return []
        if plat.drag_change_count() == self._drag_count:
            return []   # nothing new since the button went down: the last drag's files
        return plat.drag_paths()

    def _pick(self, *groups: set[str]) -> str:
        """The first wheel (in key-map order) found in the first non-empty group."""
        for group in groups:
            for wheel in self.keys:
                if wheel in group:
                    return wheel
        return ""

    def _tick(self):
        lbtn, held, esc = self._state()
        pressed = held - self._held       # keys that went down since the last tick
        pos = QCursor.pos()

        if self.menu_visible():
            if not lbtn or esc:
                # drop happened (or was cancelled) – give the drop event a moment to land
                QTimer.singleShot(120, self.hide_menu.emit)
            else:
                for key in self.repeat.feed(self._nav_state(), self.clock()):
                    self.nav_key.emit(key)
                if pressed and self.menu_got_drag():
                    self.switch_ring.emit(self._pick(pressed))
                elif not self.menu_got_drag() and (pos - self._shown_at).manhattanLength() > 6:
                    # the cursor is moving over us but no file drag arrived: it's a window
                    # move or text selection, so get out of the way
                    self._idle_ticks += 1
                    if self._idle_ticks > NO_DRAG_TIMEOUT:
                        self.hide_menu.emit()
        else:
            self.repeat.reset()
            if lbtn:
                if self._down_at is None:
                    self._down_at = pos
                    self._fired = False
                    self._stale = set(self._held)
                    if self._mac is not None and hasattr(self._mac, "drag_change_count"):
                        self._drag_count = self._mac.drag_change_count()
                self._stale &= held              # released since mouse-down: counts when pressed again
                fresh = held - self._stale
                moved = (pos - self._down_at).manhattanLength() >= DRAG_THRESHOLD
                if self.enabled and moved and fresh and not self._fired:
                    self._fired = True
                    self._idle_ticks = 0
                    self._shown_at = pos
                    # a key pressed just now beats one that was already held
                    self.show_menu.emit(pos, self._pick(pressed, fresh))
            else:
                self._down_at = None
                self._fired = False
                self._stale = set()
        self._held = held
