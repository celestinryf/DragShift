"""Design tokens (design.py), Pages-style controls (controls.py) and a smoke test that every
window, toast and the radial menu build and paint on the glass (non-macOS) path."""
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dragshift import controls, design  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def no_appkit(monkeypatch):
    """Force the Windows/Linux fallbacks, even when the tests run on a Mac."""
    monkeypatch.setattr(design, "_mac", lambda: None)
    design.invalidate()
    yield
    design.invalidate()


@pytest.fixture
def img(tmp_path) -> Path:
    p = tmp_path / "Photo.png"
    Image.new("RGB", (320, 200), (40, 120, 220)).save(p)
    return p


# ---------------------------------------------------------------- colour
def test_fallback_colours_match_apple_values(no_appkit):
    assert design.color("label", dark=False) == QColor(0, 0, 0, 216)
    assert design.color("label", dark=True) == QColor(255, 255, 255, 216)
    assert design.color("windowBackground", dark=False) == QColor(236, 236, 236)
    assert design.color("windowBackground", dark=True) == QColor(50, 50, 50)
    assert design.color("red", dark=True) == QColor(255, 69, 58)


def test_unknown_role_falls_back_to_label(no_appkit):
    assert design.color("no-such-role", dark=False) == design.color("label", dark=False)


def test_alpha_multiplies_and_returns_a_copy(no_appkit):
    c = design.color("label", dark=False, alpha=0.5)
    assert c.alpha() == 108
    c.setAlpha(1)   # mutating the result must not poison the cache
    assert design.color("label", dark=False).alpha() == 216


@pytest.mark.parametrize("dark", [False, True])
def test_every_role_is_a_valid_colour(dark):
    design.invalidate()
    for role in list(design.NS_COLORS) + list(design.FALLBACK):
        c = design.color(role, dark=dark)
        assert isinstance(c, QColor) and c.isValid(), role


@pytest.mark.skipif(sys.platform != "darwin", reason="reads NSColor")
def test_macos_reads_nscolor_for_both_appearances():
    from dragshift import platform_mac
    if not platform_mac.OK:
        pytest.skip("AppKit not importable")
    light, dark = platform_mac.ns_color("labelColor", False), platform_mac.ns_color("labelColor", True)
    assert light[:3] == (0, 0, 0) and dark[:3] == (255, 255, 255)
    assert platform_mac.ns_color("noSuchColorSelector", False) is None


def test_label_text_has_enough_contrast(no_appkit):
    """WCAG AA (4.5:1) for body text on the window background, both appearances."""
    def lum(c: QColor) -> float:
        def ch(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())

    def over(fg: QColor, bg: QColor) -> QColor:
        a = fg.alphaF()
        return QColor(*(round(f * a + b * (1 - a)) for f, b in
                        zip((fg.red(), fg.green(), fg.blue()), (bg.red(), bg.green(), bg.blue()))))
    for dark in (False, True):
        bg = design.color("windowBackground", dark=dark)
        for role in ("label", "greenText", "orangeText"):
            fg = over(design.color(role, dark=dark), bg)
            hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
            assert (hi + 0.05) / (lo + 0.05) >= 4.5 if role == "label" else 3.0, (role, dark)


def test_dark_detection_off_macos_does_not_crash(no_appkit):
    assert isinstance(design.is_dark(), bool)


# ---------------------------------------------------------------- type, motion, a11y
def test_font_uses_text_style_sizes_and_real_weights():
    f = design.font("headline")
    assert f.pixelSize() == 13 and f.weight() == QFont.Weight(600)
    assert design.font("largeTitle").pixelSize() == 26
    assert design.font("body", 700).weight() == QFont.Weight.Bold
    assert design.font("body", size=20).pixelSize() == 20
    f.setPixelSize(99)   # copies, not the cached font
    assert design.font("headline").pixelSize() == 13
    if sys.platform == "darwin":
        assert design.family() == ".AppleSystemUIFont"


def test_reduce_motion_and_transparency_are_bools_without_appkit(no_appkit):
    assert design.reduce_motion() is False
    assert design.reduce_transparency() is False
    assert design.duration("slow") == design.DURATION["slow"]


def test_reduce_motion_shortens_animations(monkeypatch):
    monkeypatch.setattr(design, "_accessibility", lambda: (True, False))
    assert design.reduce_motion() is True
    assert design.duration("slow") == design.DURATION["fast"]


def test_platform_mac_is_safe_without_appkit(monkeypatch):
    from dragshift import platform_mac
    monkeypatch.setattr(platform_mac, "OK", False)
    assert platform_mac.accessibility() == (False, False)
    assert platform_mac.ns_color("labelColor", True) is None
    assert platform_mac.symbol_png("crop", 13) is None
    assert platform_mac.is_dark() is False
    assert platform_mac.add_vibrancy(object()) is False
    assert platform_mac.native_chrome(object()) is False
    assert platform_mac.toolbar_add_group(object(), "g", [("crop", "Crop")], 2, print) is False


def test_spring_curve_settles_with_a_small_overshoot():
    vals = [design.spring_progress(i / 100) for i in range(101)]
    assert vals[0] == 0.0 and vals[-1] == 1.0
    assert max(vals) < 1.12                  # a hint of bounce, not a wobble
    assert abs(vals[95] - 1) < 0.02          # settled by the end
    c = design.spring_curve()
    assert c.valueForProgress(0.0) == 0.0 and c.valueForProgress(1.0) == 1.0


def test_spring_curve_outlives_its_caller(qapp):
    """An animation built with a throwaway spring_curve() used to segfault on start."""
    import gc

    from PySide6.QtCore import QVariantAnimation
    anim = QVariantAnimation(startValue=0.0, endValue=1.0, duration=10,
                             easingCurve=design.spring_curve(damping=0.61, response=0.37))
    gc.collect()
    anim.start()
    assert 0.0 <= anim.easingCurve().valueForProgress(0.5) <= 1.2
    anim.stop()
    assert design.spring_curve(damping=0.61, response=0.37) is design.spring_curve(damping=0.61, response=0.37)


# ---------------------------------------------------------------- SF Symbols
def test_symbol_falls_back_to_glyph_without_appkit(no_appkit):
    pm = design.symbol("crop", 17, QColor("red"), fallback="⌗")
    assert isinstance(pm, QPixmap) and not pm.isNull()
    assert design.symbol("crop", 17) is None              # no symbol, no glyph
    assert design.has_symbol("crop") is False
    assert not design.symbol_icon("crop", 13, fallback="⌗").isNull()


def test_symbol_is_cached(no_appkit):
    a = design.symbol("x", 13, QColor("blue"), fallback="✕")
    assert design.symbol("x", 13, QColor("blue"), fallback="✕") is a


@pytest.mark.skipif(sys.platform != "darwin", reason="SF Symbols are macOS only")
def test_macos_renders_sf_symbols_and_unknown_names_fall_back():
    design.invalidate()
    if design._mac() is None:
        pytest.skip("AppKit not importable")
    pm = design.symbol("crop", 17, QColor("black"))
    assert pm is not None and not pm.isNull() and pm.devicePixelRatio() >= 1
    assert design.symbol("definitely.not.a.symbol", 17) is None
    assert design.symbol("definitely.not.a.symbol", 17, fallback="?") is not None


def test_every_tool_has_a_symbol_name():
    from dragshift.app import GLYPHS
    assert set(GLYPHS) <= set(design.SYMBOLS)


# ---------------------------------------------------------------- theme + stylesheets
def test_theme_change_clears_caches_and_notifies(monkeypatch):
    t = design.theme()
    seen = []
    t.changed.connect(lambda: seen.append(True))
    flipped = not design.is_dark()
    monkeypatch.setattr(design, "_read_dark", lambda: flipped)
    t.refresh()
    assert seen and design.is_dark() is flipped
    monkeypatch.undo()
    t.refresh()


def test_stylesheets_are_built_from_tokens():
    native, glass_ = design.stylesheet(True), design.stylesheet(False)
    assert "QLabel#dim" in native and "QPushButton" not in native   # macOS keeps native controls
    assert design.css(design.color("accent")) in glass_


def test_glass_qss_is_lazy_and_valid():
    from dragshift import glass
    assert "QLabel#dim" in glass.QSS
    with pytest.raises(AttributeError):
        glass.NOT_A_THING  # noqa: B018


# ---------------------------------------------------------------- controls
def test_segmented_keeps_button_group_api():
    seg = controls.Segmented(["A", "B", "C"], 1)
    assert seg.group.checkedId() == 1
    seg.group.button(2).click()
    assert seg.group.checkedId() == 2
    assert seg.accessibleName() == "A / B / C"


def test_inspector_groups_collapse_and_footer():
    from PySide6.QtWidgets import QHBoxLayout, QLabel
    ins = controls.Inspector(tabs=["Style", "Arrange"])
    body = ins.group("Fill")
    body.addWidget(QLabel("x"))
    ins.group("Hidden", expanded=False, page=1)
    assert len(ins.headers) == 2 and ins.headers[1].isChecked() is False
    ins.headers[0].click()                     # collapse
    assert ins.headers[0].isChecked() is False
    ins.tabs.group.button(1).click()
    assert ins.stack.currentIndex() == 1
    ins.finish()
    ins.set_footer(QHBoxLayout())


def test_toolbar_buttons_are_named_and_checkable():
    tb = controls.Toolbar()
    a, b = tb.add(("rotate.left", "Rotate Left", "⟲"), ("rotate.right", "Rotate Right", "⟳"), checkable=True)
    assert a.accessibleName() == "Rotate Left" and a.isCheckable()
    a.set_label("Turned")
    assert a.text() == "Turned"
    assert a.sizeHint().height() == controls.ToolbarButton.HEIGHT


def test_colour_well_emits_choice():
    w = controls.ColorWell([("Red", ["#ff0000"]), ("Sunset", ["#ffb36b", "#a24bff"])])
    got = []
    w.picked.connect(got.append)
    w._choose(["#a24bff"])
    assert got == [["#a24bff"]] and w.colors == ["#a24bff"]


def test_popover_shape_has_an_arrow():
    pop = controls.Popover()
    pop.resize(200, 120)
    pop.arrow_x = 100
    path = pop.path()
    assert path.contains(QPointF(100, 4))          # the arrow tip area
    assert not path.contains(QPointF(20, 4))       # beside the arrow: outside


# ---------------------------------------------------------------- smoke: every surface builds and paints
def test_all_windows_build_and_paint(img, tmp_path):
    from dragshift import settings, tools
    pdf = tmp_path / "Doc.pdf"
    Image.new("RGB", (200, 200), "white").save(pdf)
    n = lambda *a, **k: None  # noqa: E731
    wins = [tools.CropWindow(str(img), n), tools.AddBgWindow(str(img), n), tools.EditWindow(str(img), n),
            tools.AnnotateWindow(str(img), n), tools.RedactWindow(str(img), n), tools.MetadataWindow(str(img), n),
            tools.CompressWindow([str(img)], n), tools.ResizeWindow([str(img)], n), tools.ProtectWindow(str(pdf), n),
            settings.SettingsWindow(settings.Settings()), settings.WelcomeWindow(settings.Settings())]
    for w in wins:
        assert not w.grab().isNull(), type(w).__name__
        if isinstance(w, tools.ToolWindow):
            assert w.primary_btn is not None and w.primary_btn.isDefault()
        w.close()


def test_tool_window_legacy_body_api_still_works(img):
    """Code written for the old single-column ToolWindow (body + footer) keeps working."""
    from PySide6.QtWidgets import QLabel
    from dragshift import tools
    w = tools.ToolWindow("Legacy", str(img), lambda *a: None)
    w.body.addWidget(QLabel("hello"))
    w.body.addLayout(w.footer("Go", w.close))
    assert w.primary_btn.text() == "Go"
    w.set_subtitle("changed")
    assert w.subtitle.text() == "changed"
    assert not w.grab().isNull()
    w.close()


def test_toasts_paint_every_state(img):
    from dragshift import report, toasts
    stack = toasts.ToastStack()
    r = report.summarize("convert:PNG", [str(img)], [str(img)])
    for fn in (lambda c: c.working("Converting…", "Photo.png"), lambda c: c.done(r),
               lambda c: c.error("Couldn't finish", "Nope"), lambda c: c.info("Hi", "There")):
        c = toasts.ToastCard(stack)
        fn(c)
        c.hovered = True
        assert not c.grab().isNull()
        c.life.stop()
        c.spinner.stop()
    assert stack._slot(0).width() == toasts.W
    card = stack.info("a", "b")
    assert card in stack.cards
    card.dismiss()


def test_radial_paints_both_rings_and_hover(img):
    from dragshift import radial
    from dragshift.radial import Item
    rings = {"Convert": [Item("PNG", key="convert:PNG"), Item("JPG", key="convert:JPG")],
             "Tools": [Item("Crop", "⌗", key="tool:Crop", hint="Trim"), Item("Edit", "☼", key="tool:Edit")]}
    m = radial.RadialMenu(lambda p: rings, lambda p: "1 KB")
    m._load([str(img)])
    m._t = m._swap = 1.0
    for ring in (0, 1):
        m.ring_idx = ring
        m.spring = [1.0, 0.0]
        m.hover = -1
        m._set_hover(0, QPointF(200, 80))
        assert not m.grab().isNull()
        assert m.accessibleName().startswith(m.items[0].label)
    m.hover = -1
    m._announce()
    assert "menu" in m.accessibleName()
    path = radial.rounded_sector_path(QPointF(200, 200), 60, 158, 10, 80, 10)
    assert radial.rounded_sector_path(QPointF(200, 200), 60, 158, 10, 80, 10) is path   # cached
    assert path.boundingRect().width() > 0


def test_toast_slot_positions(monkeypatch):
    from dragshift import toasts
    stack = toasts.ToastStack()
    monkeypatch.setattr(stack, "_screen_rect", lambda: QRect(0, 0, 1000, 800))
    a, b = stack._slot(0), stack._slot(1)
    assert a.right() <= 1000 - toasts.MARGIN
    assert abs(a.y() - b.y()) == toasts.H + toasts.GAP
