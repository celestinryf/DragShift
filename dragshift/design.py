"""Design tokens: the one place for type, colour, shape, spacing, motion and icons.

The target is Apple's Pages on macOS: the system font (SF Pro), Apple's text styles,
semantic colours read live from NSColor (so light/dark mode and the user's accent colour
follow System Settings), SF Symbols, and the inspector/toolbar metrics Pages uses.
Windows and Linux get close fallbacks from the same tokens.

Everything is cached: the radial menu paints from these on every frame."""
from __future__ import annotations

import math
import sys
import time

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPixmap

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


def _mac():
    """platform_mac when AppKit is usable, else None (tests patch this to force fallbacks)."""
    if not IS_MAC:
        return None
    try:
        from . import platform_mac
        return platform_mac if platform_mac.OK else None
    except Exception:
        return None


# ---------------------------------------------------------------- typography
# Apple's macOS text styles: (size, weight). On macOS one Qt logical pixel is one point.
TEXT_STYLES: dict[str, tuple[int, int]] = {
    "largeTitle": (26, 400), "title1": (22, 400), "title2": (17, 400), "title3": (15, 400),
    "headline": (13, 600), "body": (13, 400), "callout": (12, 400), "subheadline": (11, 400),
    "footnote": (10, 400), "caption1": (10, 400), "caption2": (10, 500),
    # Pages-specific: inspector rows use the small control size, section headers are small bold
    "inspector": (11, 400), "inspectorHeader": (11, 700), "toolbarLabel": (11, 400),
}

_family: str | None = None
_fonts: dict[tuple[str, int | None], QFont] = {}


def family() -> str:
    """The system UI font family: SF Pro on macOS, Segoe UI (Variable) on Windows."""
    global _family
    if _family is None:
        if IS_MAC:
            _family = ".AppleSystemUIFont"
        else:
            fam = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()
            if IS_WIN:
                fams = set(QFontDatabase.families())
                fam = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), fam)
            _family = fam
    return _family


def font(style: str = "body", weight: int | None = None, size: float | None = None) -> QFont:
    """A QFont for an Apple text style. Real weights (never faux bold): pass `weight` as a
    CSS-style number (400 regular, 500 medium, 600 semibold, 700 bold) to override."""
    key = (style, weight, size)
    f = _fonts.get(key)
    if f is None:
        px, w = TEXT_STYLES.get(style, TEXT_STYLES["body"])
        f = QFont(family())
        f.setPixelSize(int(round(size or px)))
        f.setWeight(QFont.Weight(weight or w))
        f.setHintingPreference(QFont.PreferNoHinting)
        _fonts[key] = f
    return QFont(f)


# ---------------------------------------------------------------- colour
# role -> NSColor selector (macOS reads these live)
NS_COLORS = {
    "label": "labelColor", "secondaryLabel": "secondaryLabelColor", "tertiaryLabel": "tertiaryLabelColor",
    "quaternaryLabel": "quaternaryLabelColor", "separator": "separatorColor",
    "windowBackground": "windowBackgroundColor", "controlBackground": "controlBackgroundColor",
    "control": "controlColor", "accent": "controlAccentColor",
    "selectedContentBackground": "selectedContentBackgroundColor",
    "focusRing": "keyboardFocusIndicatorColor", "red": "systemRedColor", "green": "systemGreenColor",
    "orange": "systemOrangeColor", "yellow": "systemYellowColor", "blue": "systemBlueColor",
}
# role -> (light, dark) RGBA; Apple's published values, used off macOS and if AppKit fails
FALLBACK: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int]]] = {
    "label": ((0, 0, 0, 216), (255, 255, 255, 216)),
    "secondaryLabel": ((0, 0, 0, 127), (255, 255, 255, 140)),
    "tertiaryLabel": ((0, 0, 0, 66), (255, 255, 255, 63)),
    "quaternaryLabel": ((0, 0, 0, 25), (255, 255, 255, 25)),
    "separator": ((0, 0, 0, 25), (255, 255, 255, 25)),
    "windowBackground": ((236, 236, 236, 255), (50, 50, 50, 255)),
    "controlBackground": ((255, 255, 255, 255), (30, 30, 30, 255)),
    "control": ((255, 255, 255, 255), (255, 255, 255, 63)),
    "accent": ((0, 122, 255, 255), (10, 132, 255, 255)),
    "selectedContentBackground": ((0, 100, 225, 255), (0, 89, 209, 255)),
    "focusRing": ((0, 103, 244, 127), (26, 169, 255, 127)),
    "red": ((255, 59, 48, 255), (255, 69, 58, 255)),
    "green": ((40, 205, 65, 255), (50, 215, 75, 255)),
    "orange": ((255, 149, 0, 255), (255, 159, 10, 255)),
    "yellow": ((255, 204, 0, 255), (255, 214, 10, 255)),
    "blue": ((0, 122, 255, 255), (10, 132, 255, 255)),
    # not NSColor roles:
    # Apple's increased-contrast system colours, for coloured *text* (plain green/orange on a
    # light window fails WCAG contrast)
    "greenText": ((36, 138, 61, 255), (48, 209, 88, 255)),
    "orangeText": ((201, 52, 0, 255), (255, 159, 10, 255)),
    # Pages' document canvas around the page, and the page shadow
    "canvas": ((222, 222, 222, 255), (30, 30, 30, 255)),
    "shadow": ((0, 0, 0, 60), (0, 0, 0, 140)),
    # glass surfaces painted by Qt (radial menu, Windows/Linux windows and toasts)
    "glassTint": ((246, 246, 246, 200), (40, 40, 40, 190)),
    "glassTintSolid": ((242, 242, 242, 246), (44, 44, 44, 246)),
    "onAccent": ((255, 255, 255, 255), (255, 255, 255, 255)),
}

_colors: dict[tuple[str, bool], QColor] = {}
_dark: bool | None = None


def is_dark() -> bool:
    """Current appearance. Cached; the cache is cleared when the system switches."""
    global _dark
    if _dark is None:
        _dark = _read_dark()
    return _dark


def _read_dark() -> bool:
    mac = _mac()
    if mac is not None:
        return mac.is_dark()
    app = QGuiApplication.instance()
    if app is None:
        return False
    try:
        scheme = app.styleHints().colorScheme()
        if scheme != Qt.ColorScheme.Unknown:
            return scheme == Qt.ColorScheme.Dark
    except AttributeError:   # Qt < 6.5
        pass
    return app.palette().window().color().lightness() < 128


def color(role: str, dark: bool | None = None, alpha: float | None = None) -> QColor:
    """A semantic colour for the current (or given) appearance. `alpha` (0..1) multiplies
    the colour's own alpha. Unknown roles fall back to the label colour."""
    dark = is_dark() if dark is None else dark
    key = (role, dark)
    c = _colors.get(key)
    if c is None:
        rgba = None
        mac = _mac()
        if mac is not None and role in NS_COLORS:
            rgba = mac.ns_color(NS_COLORS[role], dark)
        if rgba is None:
            rgba = FALLBACK.get(role, FALLBACK["label"])[1 if dark else 0]
            if role == "accent" and not IS_MAC:
                rgba = _qt_accent() or rgba
        c = QColor(*rgba)
        _colors[key] = c
    c = QColor(c)
    if alpha is not None:
        c.setAlphaF(c.alphaF() * alpha)
    return c


def _qt_accent() -> tuple[int, int, int, int] | None:
    """Windows/Linux accent colour from Qt's palette (Qt 6.6+), if the platform has one."""
    app = QGuiApplication.instance()
    try:
        from PySide6.QtGui import QPalette
        c = app.palette().color(QPalette.Accent) if app is not None else None
    except AttributeError:
        return None
    if c is None or not c.isValid():
        return None
    return c.red(), c.green(), c.blue(), 255


def css(c: QColor) -> str:
    """QColor as a Qt-stylesheet rgba() value."""
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


# ---------------------------------------------------------------- shape, spacing, sizes
RADIUS = {
    "window": 12,       # painted windows (Windows/Linux); macOS draws its own corners
    "popover": 12,
    "banner": 18,       # notification banners
    "group": 8,         # boxed groups and canvas thumbnails
    "control": 6,       # buttons, segmented controls, fields
    "small": 4,
    "petal": 10,        # radial menu segments
}
SPACING = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 20, "xxl": 24}   # 4/8-pt grid
CONTROL_HEIGHT = {"mini": 16, "small": 19, "regular": 22, "large": 28}
INSPECTOR_WIDTH = 268       # Pages' Format inspector
TOOLBAR_HEIGHT = 52         # unified toolbar band (traffic lights centred in it)
TITLEBAR_HEIGHT = 28        # plain title bar
TOOLBAR_SYMBOL = 17         # SF Symbol point size for icon-above-label toolbar buttons
HAIRLINE = 1.0              # separators are one device-independent pixel


# ---------------------------------------------------------------- accessibility
_a11y: tuple[float, bool, bool] = (0.0, False, False)
A11Y_TTL = 2.0   # seconds: System Settings changes are rare; don't ask AppKit on every frame


def _accessibility() -> tuple[bool, bool]:
    global _a11y
    now = time.monotonic()
    if now - _a11y[0] > A11Y_TTL:
        mac = _mac()
        motion, transparency = mac.accessibility() if mac is not None else (False, False)
        _a11y = (now, motion, transparency)
    return _a11y[1], _a11y[2]


def reduce_motion() -> bool:
    """System Settings > Accessibility > Display > Reduce motion (False off macOS)."""
    return bool(_accessibility()[0])


def reduce_transparency() -> bool:
    """System Settings > Accessibility > Display > Reduce transparency (False off macOS)."""
    return bool(_accessibility()[1])


# ---------------------------------------------------------------- motion
DURATION = {"fast": 150, "standard": 250, "slow": 350}   # ms


def spring_progress(t: float, damping: float = 0.78, response: float = 0.5) -> float:
    """A damped spring (SwiftUI's .spring(response:dampingFraction:)) over t in 0..1 of the
    animation's duration. Under-damped: a small, quickly settling overshoot."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    w0 = 2 * math.pi / response
    # map the animation's 0..1 onto enough seconds for the spring to settle
    secs = t * (4.6 / (damping * w0)) if damping > 0 else t
    wd = w0 * math.sqrt(max(1e-6, 1 - damping * damping))
    env = math.exp(-damping * w0 * secs)
    return 1 - env * (math.cos(wd * secs) + (damping * w0 / wd) * math.sin(wd * secs))


_SPRING_CURVES: dict[tuple[float, float], QEasingCurve] = {}


def spring_curve(damping: float = 0.78, response: float = 0.5) -> QEasingCurve:
    """PySide ties a custom easing function to the QEasingCurve that set it: copies held by an
    animation crash on start once that original is collected, so every curve stays cached."""
    c = _SPRING_CURVES.get((damping, response))
    if c is None:
        c = _SPRING_CURVES[(damping, response)] = QEasingCurve()
        c.setCustomType(lambda t: spring_progress(t, damping, response))
    return c


def duration(kind: str = "standard") -> int:
    """Animation length in ms; short cross-fades only when Reduce motion is on."""
    return DURATION["fast"] if reduce_motion() else DURATION.get(kind, DURATION["standard"])


# ---------------------------------------------------------------- SF Symbols
# Item/tool name -> SF Symbol. Names not listed keep their text glyph (and on Windows/Linux
# every item keeps its glyph).
SYMBOLS = {
    "Compress": "arrow.down.right.and.arrow.up.left", "Resize": "arrow.up.left.and.arrow.down.right",
    "Crop": "crop", "Edit": "slider.horizontal.3", "Annotate": "pencil.tip.crop.circle",
    "Redact": "eye.slash", "Add BG": "photo.artframe", "Metadata": "info.circle", "Split": "scissors",
    "Rotate": "rotate.right", "Merge": "doc.on.doc", "Protect": "lock", "Extract": "photo.on.rectangle.angled",
    "Trim": "timeline.selection", "Speed": "gauge.with.dots.needle.67percent", "Mute": "speaker.slash",
    "Audio": "waveform", "Frame": "camera", "Normalize": "speaker.wave.2", "Strip Tags": "tag.slash",
    "Cut": "scissors", "Read QR": "qrcode.viewfinder", "Reorder": "rectangle.grid.2x2",
    "Extract Files": "archivebox",
    # Actions wheel ("airdrop" isn't a public symbol, so the radio waves stand in)
    "AirDrop": "dot.radiowaves.up.forward", "Float": "pin", "Copy": "plus.square.on.square", "Move": "folder",
    "Copy Path": "link", "Terminal": "terminal", "Zip": "doc.zipper",
}

_symbol_masks: dict[tuple, QPixmap | None] = {}
_symbol_tinted: dict[tuple, QPixmap | None] = {}


def _dpr() -> float:
    app = QGuiApplication.instance()
    scr = app.primaryScreen() if app is not None else None
    return max(1.0, scr.devicePixelRatio()) if scr is not None else 2.0


def _symbol_mask(name: str, size: float, weight: str, scale: str, dpr: float) -> QPixmap | None:
    key = (name, size, weight, scale, dpr)
    if key not in _symbol_masks:
        pm = None
        mac = _mac()
        got = mac.symbol_png(name, size, weight, scale, dpr) if mac is not None and name else None
        if got is not None:
            pm = QPixmap()
            if pm.loadFromData(got[0], "PNG"):
                pm.setDevicePixelRatio(dpr)
            else:
                pm = None
        _symbol_masks[key] = pm
    return _symbol_masks[key]


def _glyph_mask(glyph: str, size: float, weight: str, dpr: float) -> QPixmap:
    weights = {"regular": 400, "medium": 500, "semibold": 600, "bold": 700}
    f = font("body", weights.get(weight, 400), size * 1.15)
    box = int(math.ceil(size * 1.5))
    pm = QPixmap(int(box * dpr), int(box * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setFont(f)
    p.setPen(QColor(0, 0, 0))
    p.drawText(QRectF(0, 0, box, box), Qt.AlignCenter, glyph)
    p.end()
    return pm


def symbol(name: str, size: float = 13, color_: QColor | None = None, weight: str = "regular",
           scale: str = "medium", fallback: str = "") -> QPixmap | None:
    """An SF Symbol as a tinted, HiDPI QPixmap (cached). Off macOS, or when the symbol
    doesn't exist on this macOS version, the `fallback` text glyph is drawn instead.
    Returns None only when there is neither."""
    dpr = _dpr()
    c = color_ if color_ is not None else color("label")
    key = (name, fallback, size, weight, scale, dpr, c.rgba())
    if key in _symbol_tinted:
        return _symbol_tinted[key]
    mask = _symbol_mask(name, size, weight, scale, dpr)
    if mask is None and fallback:
        mask = _glyph_mask(fallback, size, weight, dpr)
    out = None
    if mask is not None:
        out = QPixmap(mask.size())
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.drawPixmap(0, 0, mask)
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(out.rect(), c)
        p.end()
    _symbol_tinted[key] = out
    return out


def has_symbol(name: str) -> bool:
    """True when the SF Symbol is available (macOS 11+ with that symbol)."""
    return _symbol_mask(name, 13, "regular", "medium", _dpr()) is not None


def symbol_icon(name: str, size: float = 13, fallback: str = "", weight: str = "regular",
                role: str = "label") -> QIcon:
    """QIcon with normal / disabled / selected states tinted from semantic colours."""
    icon = QIcon()
    for mode, r in ((QIcon.Normal, role), (QIcon.Disabled, "tertiaryLabel"), (QIcon.Selected, "onAccent")):
        pm = symbol(name, size, color(r), weight, fallback=fallback)
        if pm is not None:
            icon.addPixmap(pm, mode)
    return icon


# ---------------------------------------------------------------- live theme changes
class Theme(QObject):
    """Emits `changed` when light/dark mode or the accent colour changes, after clearing
    every cache above. Windows re-style themselves from it."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        app = QGuiApplication.instance()
        self._sig = (is_dark(), color("accent").rgba())
        if app is not None:
            app.installEventFilter(self)
            try:
                app.styleHints().colorSchemeChanged.connect(lambda *_: self.refresh())
            except AttributeError:
                pass

    def eventFilter(self, obj, e):
        if e.type() == QEvent.ApplicationPaletteChange and obj is QGuiApplication.instance():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.refresh)
        return False

    def refresh(self):
        invalidate()
        sig = (is_dark(), color("accent").rgba())
        if sig != self._sig:
            self._sig = sig
            self.changed.emit()


_theme: Theme | None = None


def theme() -> Theme:
    global _theme
    if _theme is None:
        _theme = Theme()
    return _theme


def invalidate():
    """Forget cached appearance-dependent values (colours, tinted symbols, stylesheets)."""
    global _dark, _a11y
    _dark = None
    _a11y = (0.0, False, False)
    _colors.clear()
    _symbol_masks.clear()
    _symbol_tinted.clear()
    _qss.clear()


# ---------------------------------------------------------------- stylesheets
_qss: dict[tuple, str] = {}


def stylesheet(native: bool) -> str:
    """Window stylesheet. `native` (macOS): only text styles, so Qt's macOS style draws real
    AppKit buttons, pop-ups, sliders and fields. Otherwise a glass look built from the same
    tokens (Windows/Linux)."""
    key = (native, is_dark())
    if key not in _qss:
        _qss[key] = _native_qss() if native else _glass_qss()
    return _qss[key]


def _text_qss() -> str:
    lab, sec = css(color("label")), css(color("secondaryLabel"))
    return f"""
QWidget#glassRoot {{ background: transparent; }}
QLabel {{ color: {lab}; }}
QLabel#title {{ font-size: 13px; font-weight: 600; }}
QLabel#dim, QLabel.dim {{ color: {sec}; font-size: 11px; }}
QLabel#section {{ color: {lab}; font-size: 11px; font-weight: 700; }}
QLabel#big {{ font-size: 15px; font-weight: 600; }}
QLabel#good {{ color: {css(color("greenText"))}; font-weight: 600; }}
QLabel#warn {{ color: {css(color("orangeText"))}; font-weight: 600; }}
QLabel#largeTitle {{ font-size: 26px; font-weight: 700; }}
QLabel#headline {{ font-size: 13px; font-weight: 600; }}
QLabel#subtle {{ color: {sec}; font-size: 13px; }}
QLabel#inspectorLabel {{ font-size: 11px; }}
QFrame#hairline {{ background: {css(color("separator"))}; border: none; }}
QFrame#card {{ background: {css(color("quaternaryLabel", alpha=0.5))}; border: 1px solid {css(color("separator"))};
  border-radius: {RADIUS["group"]}px; }}
QLabel#preview {{ background: {css(color("canvas"))}; border-radius: {RADIUS["group"]}px; }}
"""


def _native_qss() -> str:
    return _text_qss()


def _glass_qss() -> str:
    lab, sec = css(color("label")), css(color("secondaryLabel"))
    fill = css(color("label", alpha=0.08 / 0.85))
    fill_hover = css(color("label", alpha=0.14 / 0.85))
    border = css(color("label", alpha=0.18 / 0.85))
    accent = css(color("accent"))
    field = css(color("controlBackground", alpha=0.7))
    popup = css(color("windowBackground"))
    r = RADIUS["control"]
    return _text_qss() + f"""
* {{ font-family: "{family()}"; font-size: 13px; }}
QPushButton {{ color: {lab}; background: {fill}; border: 1px solid {border}; border-radius: {r}px;
  padding: 3px 12px; min-height: 16px; }}
QPushButton:hover {{ background: {fill_hover}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:default, QPushButton#primary {{ background: {accent}; color: white; border: 1px solid {accent}; font-weight: 500; }}
QPushButton:disabled {{ color: {css(color("tertiaryLabel"))}; }}
QPushButton:focus {{ border: 2px solid {css(color("focusRing"))}; }}
QPushButton#chrome {{ background: transparent; border: none; padding: 0; color: {sec}; border-radius: {RADIUS["small"]}px; }}
QPushButton#chrome:hover {{ background: {css(color("red"))}; color: white; }}
QSlider::groove:horizontal {{ height: 4px; background: {fill_hover}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #ffffff; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
  border: 1px solid rgba(0,0,0,0.18); }}
QSpinBox, QLineEdit, QComboBox {{ color: {lab}; background: {field}; border: 1px solid {border};
  border-radius: {r}px; padding: 2px 6px; min-height: 18px; selection-background-color: {accent}; }}
QSpinBox:focus, QLineEdit:focus, QComboBox:focus {{ border: 2px solid {css(color("focusRing"))}; }}
QComboBox QAbstractItemView {{ background: {popup}; color: {lab}; border: 1px solid {border};
  selection-background-color: {accent}; selection-color: white; }}
QCheckBox {{ color: {lab}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid {border}; background: {field}; }}
QCheckBox::indicator:checked {{ background: {accent}; border: 1px solid {accent}; }}
QTableWidget {{ color: {lab}; background: {field}; border: 1px solid {border}; border-radius: {RADIUS["group"]}px;
  gridline-color: {css(color("separator"))}; }}
QHeaderView {{ background: transparent; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
QHeaderView::section {{ color: {sec}; background: transparent; border: none; padding: 4px; font-weight: 600; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {popup}; color: {lab}; border: 1px solid {border}; border-radius: {RADIUS["small"]}px; padding: 3px; }}
QMenu {{ background: {popup}; color: {lab}; border: 1px solid {border}; border-radius: {RADIUS["group"]}px; padding: 5px; }}
QMenu::item {{ padding: 3px 18px; border-radius: {RADIUS["small"]}px; }}
QMenu::item:selected {{ background: {accent}; color: white; }}
QMenu::separator {{ height: 1px; background: {css(color("separator"))}; margin: 5px 8px; }}
"""
