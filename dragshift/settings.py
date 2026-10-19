"""Persistent settings + the Settings and Welcome windows."""
from __future__ import annotations

import sys

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QSpinBox, QVBoxLayout)

from . import __version__, design
from .util import CONFIG
from .window import GlassWindow

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
APP = "DragShift"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ---------------------------------------------------------------- wheel keys
WHEELS = ("Convert", "Tools", "Actions")
KEY_SETTINGS = {"Convert": "key_convert", "Tools": "key_tools", "Actions": "key_actions"}


def key_options(platform: str = sys.platform) -> list[tuple[str, str]]:
    """(key, label) choices for the wheel pop-ups. Windows and Linux have no Command key, and
    the Windows/Super key is taken by the Start menu / desktop, so it isn't offered there."""
    if platform == "darwin":
        return [("shift", "Shift ⇧"), ("cmd", "Command ⌘"), ("ctrl", "Control ⌃"), ("alt", "Option ⌥")]
    return [("shift", "Shift"), ("ctrl", "Ctrl"), ("alt", "Alt")]


def key_glyphs(platform: str = sys.platform) -> dict[str, str]:
    """Short key names for tight spots (the radial hub)."""
    if platform == "darwin":
        return {"shift": "⇧", "cmd": "⌘", "ctrl": "⌃", "alt": "⌥"}
    return {"shift": "Shift", "ctrl": "Ctrl", "alt": "Alt"}


def default_keys(platform: str = sys.platform) -> dict[str, str]:
    """Wheel -> key. macOS follows the common layout (Shift, Command, Control); elsewhere
    Ctrl and Alt take Command's and Control's places, since there is no Command key."""
    if platform == "darwin":
        return {"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"}
    return {"Convert": "shift", "Tools": "ctrl", "Actions": "alt"}


def validate_keys(keys: dict[str, str], platform: str = sys.platform, changed: str = "") -> str | None:
    """A sentence explaining what's wrong with a wheel -> key map, or None when it's fine.
    `changed` is the wheel just edited, so a clash names the *other* wheel."""
    labels = dict(key_options(platform))
    seen: dict[str, str] = {}
    for wheel in sorted(WHEELS, key=lambda w: w == changed):
        key = keys.get(wheel)
        if key not in labels:
            return f"Choose a key for the {wheel} wheel."
        if key in seen:
            return f"{labels[key]} already opens the {seen[key]} wheel. Each wheel needs its own key."
        seen[key] = wheel
    return None


def migrate_trigger(old: str, platform: str = sys.platform) -> dict[str, str]:
    """Keys for someone upgrading from the single "trigger" setting: their key keeps opening
    Convert, and the wheel that would have shared it takes Convert's default key instead."""
    keys = default_keys(platform)
    if old not in dict(key_options(platform)):
        return keys
    for wheel in WHEELS[1:]:
        if keys[wheel] == old:
            keys[wheel] = keys["Convert"]
    keys["Convert"] = old
    return keys


DEFAULTS = {
    "output": "next",
    "jpg_quality": 90,
    "pdf_dpi": 200,
    "welcomed": False,
    "last_folder": "",        # Copy / Move destination picked last time
    **{KEY_SETTINGS[w]: k for w, k in default_keys().items()},
}


def user_store() -> QSettings:
    """The user's real preferences. A function so the tests can point every Settings() at a
    temp file instead (tests/conftest.py)."""
    return QSettings(APP, APP)


class Settings:
    def __init__(self, q: QSettings | None = None):
        self.q = q if q is not None else user_store()
        self._migrate()

    def _migrate(self):
        """Before separate wheel keys there was one "trigger" key; keep it for Convert."""
        old = self.q.value("trigger")
        if old is None:
            return
        if self.q.value(KEY_SETTINGS["Convert"]) is None:
            for wheel, key in migrate_trigger(str(old)).items():
                self.q.setValue(KEY_SETTINGS[wheel], key)
        self.q.remove("trigger")

    def key_map(self) -> dict[str, str]:
        """Wheel -> key. Damaged settings (an unknown key, two wheels on one key) fall back
        to the defaults so every wheel can still be opened."""
        keys = {w: str(self.q.value(KEY_SETTINGS[w], DEFAULTS[KEY_SETTINGS[w]])) for w in WHEELS}
        return keys if validate_keys(keys) is None else default_keys()

    def set_wheel_key(self, wheel: str, key: str) -> str | None:
        """Save `key` for `wheel`. When another wheel already uses it, the two swap keys:
        Windows and Linux offer exactly one key per wheel, so refusing would mean no key could
        ever change there. Returns the wheel that took `wheel`'s old key, or None."""
        if key not in dict(key_options()):
            raise ValueError(f"not a wheel key here: {key!r}")
        keys = self.key_map()
        other = next((w for w, k in keys.items() if k == key and w != wheel), None)
        if other is not None:
            self.set(KEY_SETTINGS[other], keys[wheel])
        self.set(KEY_SETTINGS[wheel], key)
        return other

    def key_label(self, wheel: str) -> str:
        return dict(key_options()).get(self.key_map()[wheel], "Shift")

    def get(self, key):
        v = self.q.value(key, DEFAULTS[key])
        d = DEFAULTS[key]
        if isinstance(d, bool):
            return v in (True, "true", "1", 1)
        if isinstance(d, int):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        return v

    def set(self, key, value):
        self.q.setValue(key, value)
        self.apply()

    def apply(self):
        for k in ("output", "jpg_quality", "pdf_dpi"):
            CONFIG[k] = self.get(k)


# ---------------------------------------------------------------- autostart (Windows)
def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --quiet'
    return f'"{sys.executable}" -m dragshift --quiet'


def autostart_enabled() -> bool:
    if IS_LINUX:
        from . import platform_linux
        return platform_linux.autostart_enabled()
    if IS_MAC:
        from . import platform_mac
        return platform_mac.autostart_enabled()
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    if IS_LINUX:
        from . import platform_linux
        platform_linux.set_autostart(on)
        return
    if IS_MAC:
        from . import platform_mac
        platform_mac.set_autostart(on)
        return
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


TRIGGERS = key_options()
KEY_HINT = "Press a key while you’re dragging a file to open its wheel. Press another to switch."
OUTPUTS = [("next", "Next to the original"), ("folder", "In a “DragShift” subfolder")]
DPIS = [(150, "150 dpi · small"), (200, "200 dpi · balanced"), (300, "300 dpi · print")]


def _form_label(text: str) -> QLabel:
    """Settings-window row label: right-aligned with a colon, like Pages' Settings."""
    lab = QLabel(text)
    lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lab


class SettingsWindow(GlassWindow):
    changed = Signal()

    def __init__(self, settings: Settings):
        super().__init__("Settings", size=(540, 440))
        self.s = settings
        grid = QGridLayout()
        grid.setHorizontalSpacing(design.SPACING["s"])
        grid.setVerticalSpacing(design.SPACING["m"])
        grid.setColumnStretch(1, 1)
        r = 0

        def combo(options, current, name):
            c = QComboBox()
            c.setAccessibleName(name)
            for v, label in options:
                c.addItem(label, v)
            idx = max(0, [v for v, _ in options].index(current) if current in [v for v, _ in options] else 0)
            c.setCurrentIndex(idx)
            return c

        # one pop-up per wheel; picking a key another wheel uses swaps the two (see _set_key)
        self.keys: dict[str, QComboBox] = {}
        current = self.s.key_map()
        for wheel in WHEELS:
            grid.addWidget(_form_label(f"{wheel} wheel:"), r, 0)
            c = combo(TRIGGERS, current[wheel], f"{wheel} wheel key")
            c.currentIndexChanged.connect(lambda _, w=wheel: self._set_key(w))
            grid.addWidget(c, r, 1, Qt.AlignLeft)
            self.keys[wheel] = c
            r += 1
        self.key_hint = QLabel(KEY_HINT)
        self.key_hint.setObjectName("dim")
        self.key_hint.setWordWrap(True)
        grid.addWidget(self.key_hint, r, 1)
        r += 1

        grid.addWidget(_form_label("Save results:"), r, 0)
        self.output = combo(OUTPUTS, self.s.get("output"), "Save results")
        self.output.currentIndexChanged.connect(lambda _: self._save("output", self.output.currentData()))
        grid.addWidget(self.output, r, 1, Qt.AlignLeft)
        r += 1

        grid.addWidget(_form_label("JPG and WebP quality:"), r, 0)
        row = QHBoxLayout()
        row.setSpacing(design.SPACING["s"])
        self.q = QSlider(Qt.Horizontal)
        self.q.setAccessibleName("JPG and WebP quality")
        self.q.setRange(60, 100)
        self.q.setValue(self.s.get("jpg_quality"))
        self.qv = QSpinBox()
        self.qv.setRange(60, 100)
        self.qv.setValue(self.q.value())
        self.qv.setAccessibleName("JPG and WebP quality")
        self.qv.valueChanged.connect(self.q.setValue)
        self.q.valueChanged.connect(lambda v: (self.qv.setValue(v), self._save("jpg_quality", v)))
        row.addWidget(self.q, 1)
        row.addWidget(self.qv)
        grid.addLayout(row, r, 1)
        r += 1

        grid.addWidget(_form_label("PDF to image resolution:"), r, 0)
        self.dpi = combo(DPIS, self.s.get("pdf_dpi"), "PDF to image resolution")
        self.dpi.currentIndexChanged.connect(lambda _: self._save("pdf_dpi", self.dpi.currentData()))
        grid.addWidget(self.dpi, r, 1, Qt.AlignLeft)
        r += 1

        if IS_WIN or IS_MAC or IS_LINUX:
            grid.addWidget(_form_label("Startup:"), r, 0)
            self.auto = QCheckBox("Start DragShift with Windows" if IS_WIN else "Open DragShift at login")
            self.auto.setChecked(autostart_enabled())
            self.auto.toggled.connect(set_autostart)
            grid.addWidget(self.auto, r, 1)
            r += 1
        if IS_MAC:
            from . import platform_mac
            if not platform_mac.has_screen_recording():
                glass_btn = QPushButton("Enable Full Liquid Glass…")
                glass_btn.setToolTip("Lets the glass refract the windows behind it (Screen Recording permission)")
                glass_btn.clicked.connect(platform_mac.request_screen_recording)
                grid.addWidget(glass_btn, r, 1, Qt.AlignLeft)
                r += 1
        self.body.addLayout(grid)
        self.body.addStretch(1)
        about = QLabel(f"DragShift {__version__}  ·  originals are never modified")
        about.setObjectName("dim")
        foot = QHBoxLayout()
        foot.addWidget(about)
        foot.addStretch(1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setDefault(True)
        done.clicked.connect(self.close)
        foot.addWidget(done)
        self.body.addLayout(foot)

    def _save(self, key, value):
        self.s.set(key, value)
        self.changed.emit()

    def _set_key(self, wheel: str):
        other = self.s.set_wheel_key(wheel, self.keys[wheel].currentData())
        if other is None:
            self.key_hint.setText(KEY_HINT)
        else:
            # show the swap in the other wheel's pop-up too, without re-entering this handler
            c = self.keys[other]
            c.blockSignals(True)
            c.setCurrentIndex(max(0, c.findData(self.s.key_map()[other])))
            c.blockSignals(False)
            self.key_hint.setText(f"Swapped: {self.s.key_label(other)} now opens the {other} wheel.")
        self.changed.emit()


class WelcomeWindow(GlassWindow):
    """Apple's welcome-sheet layout: large title, then features with an accent SF Symbol,
    a headline and a secondary line each, and a Continue button."""

    def __init__(self, settings: Settings):
        super().__init__("Welcome to DragShift", size=(560, 520), show_title=False)
        self.s = settings
        title = QLabel("Welcome to DragShift")
        title.setObjectName("largeTitle")
        title.setAlignment(Qt.AlignHCenter)
        self.body.addSpacing(design.SPACING["s"])
        self.body.addWidget(title)
        self.body.addSpacing(design.SPACING["l"])
        steps = [
            ("hand.draw", "☝", "Start dragging any file", "in Finder or on the desktop" if IS_MAC else
             "in your file manager (Thunar, Nautilus…)" if IS_LINUX else "in File Explorer or on the desktop"),
            ("shift", "⇧", f"Press {settings.key_label('Convert')}", "the Convert wheel opens right under your cursor"),
            ("circle.circle", "◎", "Drop it on a slice", "the result is saved next to the original"),
            ("arrow.left.arrow.right", "⇄",
             f"{settings.key_label('Tools')} for Tools, {settings.key_label('Actions')} for Actions",
             "press one while the wheel is open to switch; Actions work on any file or folder"),
        ]
        steps_box = QVBoxLayout()
        steps_box.setSpacing(design.SPACING["l"])
        steps_box.setContentsMargins(design.SPACING["xxl"], 0, design.SPACING["xxl"], 0)
        for sym, glyph, head, sub in steps:
            row = QHBoxLayout()
            row.setSpacing(design.SPACING["l"])
            icon = QLabel()
            icon.setFixedSize(40, 36)
            icon.setAlignment(Qt.AlignCenter)
            pm = design.symbol(sym, 26, design.color("accent"), fallback=glyph)
            if pm is not None:
                icon.setPixmap(pm)
            icon.setAccessibleName(head)
            row.addWidget(icon, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(design.SPACING["xxs"])
            h = QLabel(head)
            h.setObjectName("headline")
            d = QLabel(sub)
            d.setObjectName("subtle")
            d.setWordWrap(True)
            col.addWidget(h)
            col.addWidget(d)
            row.addLayout(col, 1)
            steps_box.addLayout(row)
        self.body.addLayout(steps_box)
        self.body.addStretch(1)
        if IS_MAC:
            from . import platform_mac
            if not platform_mac.has_screen_recording():
                note = QLabel("Optional: allow Screen Recording so the glass can refract the windows "
                              "behind it (otherwise it shows your wallpaper). DragShift never saves or sends it.")
                note.setObjectName("dim")
                note.setWordWrap(True)
                self.body.addWidget(note)
                b = QPushButton("Allow Screen Recording…")
                b.clicked.connect(platform_mac.request_screen_recording)
                self.body.addWidget(b, 0, Qt.AlignLeft)
        if IS_LINUX:
            from . import platform_linux
            if platform_linux.is_wayland():
                note = QLabel("You're on a Wayland session: the Shift-while-dragging trigger needs X11 (e.g. XFCE). "
                              "Right-click a file → Open With → DragShift still works.")
                note.setObjectName("warn")
                note.setWordWrap(True)
                self.body.addWidget(note)
        foot = QHBoxLayout()
        if IS_WIN or IS_MAC or IS_LINUX:
            self.auto = QCheckBox("Start with Windows" if IS_WIN else "Open at login")
            self.auto.setChecked(True)
            foot.addWidget(self.auto)
        foot.addStretch(1)
        ok = QPushButton("Continue")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.setMinimumWidth(120)
        ok.clicked.connect(self._done)
        foot.addWidget(ok)
        self.body.addLayout(foot)

    def _done(self):
        self.s.set("welcomed", True)
        if IS_WIN or IS_MAC or IS_LINUX:
            set_autostart(self.auto.isChecked())
        self.close()
