"""Workstream 6: keyboard control of the wheel (arrows, Enter, Escape, Tab), key repeat for the
polled keys during a drag, the focused click menu, and the Finder Services entry."""
import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dragshift import platform_linux, platform_mac, radial, trigger  # noqa: E402
from dragshift.radial import Item, RadialMenu, step  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qapp():
    yield QApplication.instance() or QApplication([])


def pump(ms=30):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@pytest.fixture
def menu(qapp, monkeypatch):
    # click menus take focus on macOS: never activate the test process or another app
    monkeypatch.setattr(platform_mac, "activate", lambda: None)
    monkeypatch.setattr(platform_mac, "frontmost_app", lambda: None)
    m = RadialMenu(lambda p: {}, lambda p: "2 files")
    m.rings = {"Convert": [Item("PNG", hint="Lossless"), Item("JPG"), Item("WEBP"), Item("HEIC")],
               "Tools": [Item("Crop", glyph="⌗"), Item("Resize", glyph="⤢")],
               "Actions": [Item("Copy", glyph="⧉")]}
    m.ring_names = list(m.rings)
    m.ring_idx = 0
    m.paths = ["/tmp/a.png", "/tmp/b.png"]
    m.spring = [0.0] * len(m.items)
    ran = []
    m.activated.connect(lambda item, paths: ran.append((item.label, paths)))
    m.ran = ran
    yield m
    m.hub_timer.stop()
    m.hide()
    m.deleteLater()


def press(m, *keys):
    for k in keys:
        m.nav(k)


# ---------------------------------------------------------------- petal navigation math
@pytest.mark.parametrize("index,count,delta,want", [
    (0, 4, 1, 1), (3, 4, 1, 0),        # clockwise, wraps past the last petal
    (0, 4, -1, 3), (2, 4, -1, 1),      # counter-clockwise, wraps past the first
    (-1, 4, 1, 0), (-1, 4, -1, 3),     # nothing highlighted: Right starts at the top, Left at the end
    (-2, 4, 1, 0),                     # the hub counts as nothing highlighted
    (0, 1, 1, 0), (0, 1, -1, 0),       # one petal: stays put
    (-1, 0, 1, -1), (2, 0, -1, -1),    # empty ring: nothing to highlight
])
def test_step(index, count, delta, want):
    assert step(index, count, delta) == want


def test_right_and_left_move_round_the_wheel(menu):
    press(menu, "right")
    assert menu.hover == 0
    press(menu, "right", "right", "right")
    assert menu.hover == 3
    press(menu, "right")
    assert menu.hover == 0
    press(menu, "left")
    assert menu.hover == 3


def test_down_and_up_switch_wheels_keeping_the_position(menu):
    press(menu, "right", "right")            # JPG
    press(menu, "down")
    assert menu.ring_names[menu.ring_idx] == "Tools" and menu.hover == 1
    press(menu, "down")                      # Actions has one petal: clamp to it
    assert menu.ring_names[menu.ring_idx] == "Actions" and menu.hover == 0
    press(menu, "down")                      # wraps to the first wheel
    assert menu.ring_names[menu.ring_idx] == "Convert" and menu.hover == 0
    press(menu, "up")
    assert menu.ring_names[menu.ring_idx] == "Actions"


def test_wheel_switch_without_a_highlight_stays_unhighlighted(menu):
    press(menu, "tab")
    assert menu.ring_names[menu.ring_idx] == "Tools" and menu.hover == -1


def test_single_ring_ignores_up_and_down(menu):
    menu.rings = {"Actions": [Item("Copy")]}
    menu.ring_names, menu.ring_idx = ["Actions"], 0
    press(menu, "right", "down", "up", "right")
    assert menu.ring_idx == 0 and menu.hover == 0


def test_no_rings_yet_is_harmless(menu):
    menu.rings, menu.ring_names, menu.ring_idx = {}, [], 0
    press(menu, "right", "left", "down", "enter")
    assert menu.hover == -1 and menu.ran == []


def test_empty_ring_highlights_nothing(menu):
    menu.rings["Tools"] = []
    menu.ring_idx = 1
    press(menu, "right", "left")
    assert menu.hover == -1


# ---------------------------------------------------------------- looks like a hover
def test_keyboard_highlight_is_the_hover_highlight(menu):
    press(menu, "right", "right")
    assert menu.hover == 1 and menu.kbd
    # the light gathers at the petal's centre, which lies inside that petal
    assert menu.hit(menu.cursor_pos) == 1
    for _ in range(40):
        menu._tick()
    assert menu.spring[1] == 1.0 and menu.spring[0] == 0.0
    assert menu.hub_lines() == ("JPG", "")


def test_keyboard_highlight_updates_the_voiceover_name(menu):
    press(menu, "right")
    assert menu.accessibleName() == "PNG, Lossless"
    press(menu, "right")
    assert menu.accessibleName() == "JPG"
    assert "PNG" in menu.accessibleDescription()


def test_still_pointer_does_not_steal_the_keyboard_highlight(menu):
    menu.last_pointer = QPointF(radial.SIZE / 2, radial.SIZE / 2)
    press(menu, "right", "right")
    menu._pointer(QPointF(radial.SIZE / 2 + 1, radial.SIZE / 2))   # repeated drag-move, no real motion
    assert menu.hover == 1 and menu.kbd


def test_moving_the_pointer_takes_the_highlight_back(menu):
    press(menu, "right", "right")
    top = QPointF(radial.SIZE / 2, radial.SIZE / 2 - (radial.R_IN + radial.R_OUT) / 2)
    menu._pointer(top)
    assert menu.hover == 0 and not menu.kbd


def test_drop_without_moving_runs_the_keyboard_pick(menu):
    menu.last_pointer = QPointF(radial.SIZE / 2, radial.SIZE / 2)
    press(menu, "right", "right")
    assert menu.target(menu.last_pointer) == 1          # the hub is under the pointer, JPG is picked
    menu.kbd = False
    assert menu.target(menu.last_pointer) == -2


# ---------------------------------------------------------------- Enter / Escape
def test_enter_in_a_click_menu_runs_and_closes(menu):
    menu.click_mode = True
    menu.show()
    press(menu, "right", "enter")
    pump()
    assert menu.ran == [("PNG", ["/tmp/a.png", "/tmp/b.png"])]
    assert not menu.isVisible()


def test_enter_mid_drag_runs_now_and_waits_for_the_drop(menu):
    menu.click_mode = False
    menu.show()
    press(menu, "left", "enter")
    pump()
    assert menu.ran == [("HEIC", ["/tmp/a.png", "/tmp/b.png"])]
    assert menu.isVisible()                               # a harmless place to let go
    assert menu.hub_lines() == ("HEIC", radial.RELEASE_HINT)
    assert menu.accessibleName() == f"HEIC, {radial.RELEASE_HINT}"
    press(menu, "right", "down", "enter")                 # already ran: nothing else happens
    menu._pointer(QPointF(10, 10))
    pump()
    assert len(menu.ran) == 1 and menu.hover == 3


def test_drop_after_enter_is_swallowed_as_a_copy(menu):
    menu.show()
    press(menu, "right", "enter")
    pump()

    class Drop:
        action = None
        accepted = False

        def setDropAction(self, a):
            self.action = a

        def accept(self):
            self.accepted = True

        def position(self):
            return QPointF(10, 10)

    e = Drop()
    menu.dropEvent(e)
    pump()
    assert e.accepted and e.action == Qt.CopyAction
    assert not menu.isVisible() and len(menu.ran) == 1


def test_enter_with_nothing_highlighted_does_nothing(menu):
    menu.show()
    press(menu, "enter")
    pump()
    assert menu.ran == [] and menu.committed is None


def test_enter_on_the_hub_shows_the_next_wheel(menu):
    menu._set_hover(-2)
    press(menu, "enter")
    assert menu.ring_names[menu.ring_idx] == "Tools"


@pytest.mark.parametrize("click_mode", [True, False])
def test_escape_closes_in_both_modes(menu, click_mode):
    menu.click_mode = click_mode
    menu.show()
    closed = []
    menu.dismissed.connect(lambda: closed.append(True))
    press(menu, "right", "escape")
    assert closed == [True] and not menu.isVisible() and menu.ran == []


def test_escape_hands_focus_back_after_a_click_menu(menu, monkeypatch):
    back = []
    monkeypatch.setattr(platform_mac, "reactivate", lambda app: back.append(app))
    monkeypatch.setattr(radial.sys, "platform", "darwin")
    menu.click_mode = True
    menu._return_to = "Finder"
    menu.show()
    press(menu, "escape")
    assert back == ["Finder"] and menu._return_to is None


# ---------------------------------------------------------------- focused click menu
def key(m, k, mods=Qt.NoModifier):
    m.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, mods))


def test_key_press_events_drive_the_click_menu(menu):
    menu.click_mode = True
    menu.show()
    key(menu, Qt.Key_Right)
    key(menu, Qt.Key_Right)
    assert menu.hover == 1
    key(menu, Qt.Key_Left)
    assert menu.hover == 0
    key(menu, Qt.Key_Down)
    assert menu.ring_names[menu.ring_idx] == "Tools" and menu.hover == 0
    key(menu, Qt.Key_Up)
    assert menu.ring_names[menu.ring_idx] == "Convert"
    key(menu, Qt.Key_Enter)                       # keypad Enter
    pump()
    assert menu.ran == [("PNG", ["/tmp/a.png", "/tmp/b.png"])] and not menu.isVisible()


def test_return_and_escape_keys_in_the_click_menu(menu):
    menu.click_mode = True
    menu.show()
    key(menu, Qt.Key_Escape)
    assert not menu.isVisible()
    menu.show()
    key(menu, Qt.Key_Right)
    key(menu, Qt.Key_Return)
    pump()
    assert menu.ran == [("PNG", ["/tmp/a.png", "/tmp/b.png"])]


def test_tab_switches_wheels_and_never_moves_focus(menu):
    key(menu, Qt.Key_Tab)
    assert menu.ring_names[menu.ring_idx] == "Tools"
    assert menu.focusNextPrevChild(True) is False


@pytest.mark.parametrize("platform,qt_key,wheel", [
    ("darwin", Qt.Key_Control, "Tools"),     # Qt calls Command "Control" on macOS
    ("darwin", Qt.Key_Meta, "Actions"),      # ...and Control "Meta"
    ("win32", Qt.Key_Control, "Actions"),
    ("linux", Qt.Key_Shift, "Convert"),
])
def test_wheel_keys_jump_to_their_wheel_in_the_click_menu(menu, monkeypatch, platform, qt_key, wheel):
    monkeypatch.setattr(radial.sys, "platform", platform)
    menu.wheel_keys = {"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"}
    menu.ring_idx = 1 if wheel == "Convert" else 0
    key(menu, qt_key)
    assert menu.ring_names[menu.ring_idx] == wheel


def test_shift_still_cycles_when_no_keys_are_mapped(menu):
    menu.wheel_keys = {}
    key(menu, Qt.Key_Shift)
    assert menu.ring_names[menu.ring_idx] == "Tools"


def test_click_menu_takes_focus_and_drag_menu_never_does(menu):
    menu.popup(QPoint(300, 300), ["/tmp/a.png"])
    assert not menu.windowFlags() & Qt.WindowDoesNotAcceptFocus
    assert not menu.testAttribute(Qt.WA_ShowWithoutActivating)
    assert menu.focusPolicy() == Qt.StrongFocus
    menu.hide()
    menu.popup(QPoint(300, 300))
    assert menu.windowFlags() & Qt.WindowDoesNotAcceptFocus
    assert menu.testAttribute(Qt.WA_ShowWithoutActivating)
    assert not menu.click_mode


def test_popup_resets_keyboard_state(menu):
    press(menu, "right", "enter")
    menu.popup(QPoint(300, 300))
    assert menu.hover == -1 and not menu.kbd and menu.committed is None


def test_popup_with_dragged_files_loads_the_rings_at_once(qapp):
    m = RadialMenu(lambda p: {"Convert": [Item("JPG")], "Actions": [Item("Copy")]}, lambda p: "1 file")
    m.popup(QPoint(300, 300), ring="Actions", dragged=["/tmp/x.png"])
    try:
        assert not m.click_mode and m.got_drag
        assert m.paths == ["/tmp/x.png"] and m.ring_names[m.ring_idx] == "Actions"
        m.nav("right")
        assert m.hover == 0
    finally:
        m.hide()
        m.deleteLater()


# ---------------------------------------------------------------- key repeat (fake clock)
def test_key_repeat_waits_the_delay_then_repeats():
    r = trigger.KeyRepeat(delay=0.4, interval=0.08)
    assert r.feed({"right"}, 0.0) == ["right"]
    assert r.feed({"right"}, 0.2) == []
    assert r.feed({"right"}, 0.39) == []
    assert r.feed({"right"}, 0.40) == ["right"]
    assert r.feed({"right"}, 0.45) == []
    assert r.feed({"right"}, 0.48) == ["right"]
    assert r.feed({"right"}, 0.56) == ["right"]


def test_key_repeat_restarts_after_release():
    r = trigger.KeyRepeat(delay=0.4, interval=0.08)
    r.feed({"left"}, 0.0)
    assert r.feed(set(), 0.1) == []
    assert r.feed({"left"}, 0.2) == ["left"]
    assert r.feed({"left"}, 0.5) == []          # a fresh delay from the new press
    assert r.feed({"left"}, 0.6) == ["left"]


def test_late_tick_fires_once_instead_of_bursting():
    r = trigger.KeyRepeat(delay=0.4, interval=0.08)
    r.feed({"down"}, 0.0)
    assert r.feed({"down"}, 2.0) == ["down"]
    assert r.feed({"down"}, 2.01) == []
    assert r.feed({"down"}, 2.08) == ["down"]


def test_enter_and_tab_never_repeat():
    r = trigger.KeyRepeat(delay=0.4, interval=0.08)
    assert r.feed({"enter", "tab"}, 0.0) == ["enter", "tab"]
    assert r.feed({"enter", "tab"}, 5.0) == []


def test_several_keys_fire_in_a_stable_order():
    r = trigger.KeyRepeat()
    assert r.feed({"enter", "right", "left"}, 0.0) == ["left", "right", "enter"]


def test_reset_forgets_held_keys():
    r = trigger.KeyRepeat()
    r.feed({"right"}, 0.0)
    r.reset()
    assert r.feed({"right"}, 0.01) == ["right"]


def test_repeat_timing_follows_the_platform_setting():
    class Plat:
        key_repeat = staticmethod(lambda: (0.5, 0.1))
    r = trigger._repeat_for(Plat)
    assert (r.delay, r.interval) == (0.5, 0.1)
    d = trigger._repeat_for(None)
    assert (d.delay, d.interval) == (0.4, 0.08)


# ---------------------------------------------------------------- the watcher polls the keys mid-drag
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def watch(qapp, monkeypatch):
    menu = {"visible": True, "got_drag": True}
    w = trigger.DragShiftWatcher(lambda: menu["visible"], lambda: menu["got_drag"])
    w.set_keys({"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"})
    w.repeat = trigger.KeyRepeat(delay=0.4, interval=0.08)
    w.clock = clock = Clock()
    state = {"lbtn": True, "esc": False, "nav": set()}
    w._state = lambda: (state["lbtn"], set(), state["esc"])
    w._nav_state = lambda: set(state["nav"])
    monkeypatch.setattr(trigger.QCursor, "pos", staticmethod(lambda: QPoint(0, 0)))
    keys, hidden = [], []
    w.nav_key.connect(keys.append)
    w.hide_menu.connect(lambda: hidden.append(True))
    return w, state, clock, menu, keys, hidden


def test_watcher_turns_held_arrows_into_repeated_presses(watch):
    w, state, clock, menu, keys, hidden = watch
    state["nav"] = {"right"}
    for t in (0.0, 0.015, 0.2, 0.4, 0.45, 0.48):
        clock.t = t
        w._tick()
    assert keys == ["right", "right", "right"]


def test_watcher_sends_enter_once(watch):
    w, state, clock, menu, keys, hidden = watch
    state["nav"] = {"enter"}
    for t in (0.0, 0.5, 1.0):
        clock.t = t
        w._tick()
    assert keys == ["enter"]


def test_watcher_ignores_nav_keys_while_the_menu_is_closed(watch):
    w, state, clock, menu, keys, hidden = watch
    menu["visible"] = False
    state["nav"] = {"right", "enter"}
    w._tick()
    assert keys == []


def test_watcher_escape_hides_the_menu(watch):
    w, state, clock, menu, keys, hidden = watch
    state["esc"] = True
    w._tick()
    pump(200)
    assert hidden and keys == []


@pytest.mark.parametrize("held,want", [({123}, {"left"}), ({36}, {"enter"}), ({76}, {"enter"}),
                                       ({125, 48}, {"down", "tab"})])
def test_mac_nav_state_reads_key_codes(monkeypatch, held, want):
    if not platform_mac.OK:
        pytest.skip("needs pyobjc")
    monkeypatch.setattr(platform_mac.Quartz, "CGEventSourceKeyState", lambda src, code: code in held)
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)
    w._mac, w._user32 = platform_mac, None
    assert w._nav_state() == want


def test_windows_nav_state_reads_virtual_keys():
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)

    class User32:
        @staticmethod
        def GetAsyncKeyState(vk):
            return 0x8000 if vk in (0x25, 0x0D) else 0
    w._mac, w._user32 = None, User32
    assert w._nav_state() == {"left", "enter"}


@pytest.mark.parametrize("vk", [0x5B, 0x5C])
def test_windows_command_key_is_either_windows_key(vk):
    """Left (0x5B) or right (0x5C) Windows key both count as "cmd"; neither means not held."""
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)

    class User32:
        down = set()

        @classmethod
        def GetAsyncKeyState(cls, code):
            return 0x8000 if code in cls.down else 0
    w._mac, w._user32 = None, User32
    assert not w._key_down("cmd")
    User32.down = {vk}
    assert w._key_down("cmd") and not w._key_down("ctrl")


def test_platform_key_tables_cover_every_nav_key():
    for name in trigger.NAV_KEYS + ("escape",):
        assert platform_mac.KEY_CODES[name]
        assert platform_linux.KEYSYMS[name]
    assert set(trigger.NAV_VK) == set(trigger.NAV_KEYS)
    assert platform_mac.KEY_CODES["enter"] == (36, 76) and platform_mac.KEY_CODES["escape"] == (53,)


def test_escape_uses_the_same_key_table(monkeypatch):
    if not platform_mac.OK:
        pytest.skip("needs pyobjc")
    monkeypatch.setattr(platform_mac.Quartz, "CGEventSourceKeyState", lambda src, code: code == 53)
    assert platform_mac.escape_down() and not platform_mac.nav_down("enter")


# ---------------------------------------------------------------- drag pasteboard freshness
class FakeDragPlatform:
    def __init__(self):
        self.count, self.paths = 5, ["/old/drag.png"]
    drag_change_count = lambda self: self.count  # noqa: E731
    drag_paths = lambda self: list(self.paths)   # noqa: E731
    left_button_down = staticmethod(lambda: True)
    key_down = staticmethod(lambda name: False)
    escape_down = staticmethod(lambda: False)
    nav_down = staticmethod(lambda name: False)


def test_drag_paths_only_when_a_new_drag_started(qapp, monkeypatch):
    plat = FakeDragPlatform()
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)
    w._mac = plat
    assert w.drag_paths() == []                  # no mouse-down seen yet
    monkeypatch.setattr(trigger.QCursor, "pos", staticmethod(lambda: QPoint(0, 0)))
    w._tick()                                    # button goes down: remember the count
    assert w.drag_paths() == []                  # still the last drag's files
    plat.count, plat.paths = 6, ["/new/a.png"]
    assert w.drag_paths() == ["/new/a.png"]


def test_drag_paths_empty_without_a_drag_pasteboard():
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)
    w._mac = None
    assert w.drag_paths() == []


# ---------------------------------------------------------------- macOS pasteboards and Services
mac_only = pytest.mark.skipif(sys.platform != "darwin" or not platform_mac.OK, reason="macOS + pyobjc")


def _pasteboard_with(paths):
    import AppKit
    pb = AppKit.NSPasteboard.pasteboardWithUniqueName()
    pb.clearContents()
    pb.writeObjects_([AppKit.NSURL.fileURLWithPath_(p) for p in paths])
    return pb


@mac_only
def test_pasteboard_paths_reads_file_urls(tmp_path):
    import AppKit
    a, b = tmp_path / "a b.png", tmp_path / "c.txt"
    a.write_bytes(b"x")
    b.write_text("y")
    pb = _pasteboard_with([str(a), str(b)])
    try:
        assert platform_mac.pasteboard_paths(pb) == [str(a), str(b)]
        # Finder writes file reference URLs (file:///.file/id=...): they resolve to paths too
        ref = AppKit.NSURL.fileURLWithPath_(str(a)).fileReferenceURL()
        pb.clearContents()
        pb.writeObjects_([ref])
        assert [Path(p).resolve() for p in platform_mac.pasteboard_paths(pb)] == [a.resolve()]
    finally:
        pb.releaseGlobally()


@mac_only
def test_pasteboard_without_files_gives_nothing():
    import AppKit
    pb = AppKit.NSPasteboard.pasteboardWithUniqueName()
    pb.clearContents()
    pb.setString_forType_("hello", AppKit.NSPasteboardTypeString)
    try:
        assert platform_mac.pasteboard_paths(pb) == []
    finally:
        pb.releaseGlobally()


@mac_only
def test_drag_pasteboard_is_readable():
    assert platform_mac.drag_change_count() >= 0
    assert isinstance(platform_mac.drag_paths(), list)


@mac_only
def test_services_provider_hands_files_over(tmp_path, monkeypatch):
    got = []
    monkeypatch.setattr(platform_mac, "_ON_SERVICE_FILES", got.append)
    f = tmp_path / "photo.heic"
    f.write_bytes(b"x")
    pb = _pasteboard_with([str(f)])
    try:
        provider = platform_mac._services_class().alloc().init()
        provider.openFiles_userData_error_(pb, None, None)
        assert got == [[str(f)]]
    finally:
        pb.releaseGlobally()


@mac_only
def test_services_provider_survives_a_failing_callback(tmp_path, monkeypatch):
    def boom(paths):
        raise RuntimeError("callback failed")
    monkeypatch.setattr(platform_mac, "_ON_SERVICE_FILES", boom)
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    pb = _pasteboard_with([str(f)])
    try:
        platform_mac._services_class().alloc().init().openFiles_userData_error_(pb, None, None)
    finally:
        pb.releaseGlobally()


@mac_only
def test_register_services_installs_one_provider(qapp, monkeypatch):
    import AppKit
    refreshed = []
    monkeypatch.setattr(platform_mac.AppKit, "NSUpdateDynamicServices", lambda: refreshed.append(True))
    monkeypatch.setattr(platform_mac, "_SERVICES", None)
    monkeypatch.setattr(platform_mac, "_ON_SERVICE_FILES", None)
    first, second = [], []
    assert platform_mac.register_services(first.append)
    provider = platform_mac._SERVICES
    assert AppKit.NSApplication.sharedApplication().servicesProvider() is provider
    assert platform_mac.register_services(second.append)     # again: new callback, same provider
    assert platform_mac._SERVICES is provider and refreshed == [True]
    assert platform_mac._ON_SERVICE_FILES == second.append


def test_service_message_matches_the_provider_method():
    assert platform_mac.SERVICE_MESSAGE == "openFiles"
    if platform_mac.OK:
        cls = platform_mac._services_class()
        # AppKit calls -openFiles:userData:error: with an NSString ** out parameter
        assert cls.openFiles_userData_error_.signature == b"v@:@@o^@"
        assert cls.alloc().init().respondsToSelector_(platform_mac.SERVICE_MESSAGE + ":userData:error:")


# ---------------------------------------------------------------- Info.plist in the spec
def _info_plist() -> dict:
    tree = ast.parse((ROOT / "DragShift.spec").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "info_plist":
            return ast.literal_eval(node.value)
    raise AssertionError("no info_plist in DragShift.spec")


def test_spec_declares_the_finder_service():
    services = _info_plist()["NSServices"]
    assert len(services) == 1
    s = services[0]
    assert s["NSMessage"] == platform_mac.SERVICE_MESSAGE
    assert s["NSPortName"] == _info_plist()["CFBundleName"]
    assert s["NSMenuItem"]["default"] == "Open in DragShift"
    assert s["NSSendFileTypes"] == ["public.item"]       # files and folders alike
    assert s["NSRequiredContext"] == {}                  # enabled by default
    assert "NSKeyEquivalent" not in s                    # the user picks the shortcut


def test_spec_asks_for_no_privacy_permissions():
    plist = _info_plist()
    assert not [k for k in plist if k.endswith("UsageDescription")]
