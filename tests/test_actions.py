"""Workstream 3: a key per wheel (Settings, migration, the watcher) and the Actions wheel
(ring availability, Copy, Move + Undo, Copy Path, Terminal, AirDrop, Float)."""
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dragshift import actions, floating, report, settings, toasts, trigger  # noqa: E402
from dragshift.app import DragShiftApp, GLYPHS, HINTS  # noqa: E402
from dragshift.radial import RadialMenu  # noqa: E402
from dragshift.util import UserError  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def prefs(tmp_path):
    """Settings backed by a throwaway ini file, never the user's real preferences."""
    def make(**values):
        q = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
        for k, v in values.items():
            q.setValue(k, v)
        q.sync()
        return settings.Settings(q)
    return make


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    img = src / "Photo.png"
    Image.new("RGB", (40, 30), (200, 80, 40)).save(img)
    pdf = src / "Doc.pdf"
    Image.new("RGB", (40, 40), "white").save(pdf)
    folder = src / "Project"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("a")
    (folder / "sub" / "b.txt").write_text("b")
    odd = src / "data.xyz"
    odd.write_bytes(b"\x00\x01")
    dest = tmp_path / "dest"
    dest.mkdir()
    return {"img": img, "pdf": pdf, "folder": folder, "odd": odd, "dest": dest, "src": src}


# ---------------------------------------------------------------- key map: defaults, labels, validation
def test_mac_defaults_match_the_common_layout():
    assert settings.default_keys("darwin") == {"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"}
    assert [k for k, _ in settings.key_options("darwin")] == ["shift", "cmd", "ctrl", "alt"]
    assert settings.key_glyphs("darwin")["cmd"] == "⌘"


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_windows_and_linux_defaults_have_no_command_key(platform):
    keys = settings.default_keys(platform)
    assert keys == {"Convert": "shift", "Tools": "ctrl", "Actions": "alt"}
    assert "cmd" not in dict(settings.key_options(platform))
    assert settings.validate_keys(keys, platform) is None


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_defaults_are_valid_and_distinct(platform):
    keys = settings.default_keys(platform)
    assert set(keys) == set(settings.WHEELS)
    assert len(set(keys.values())) == 3
    assert settings.validate_keys(keys, platform) is None


def test_duplicate_keys_are_refused_with_a_reason():
    msg = settings.validate_keys({"Convert": "shift", "Tools": "shift", "Actions": "ctrl"}, "darwin")
    assert msg and "Shift" in msg and "Convert" in msg


def test_unknown_or_missing_key_is_refused():
    assert settings.validate_keys({"Convert": "shift", "Tools": "cmd", "Actions": "hyper"}, "darwin")
    assert settings.validate_keys({"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"}, "win32")  # no cmd
    assert settings.validate_keys({"Convert": "shift"}, "darwin")


# ---------------------------------------------------------------- migration from the single trigger
@pytest.mark.parametrize("platform,old,want", [
    ("darwin", "shift", {"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"}),
    ("darwin", "ctrl", {"Convert": "ctrl", "Tools": "cmd", "Actions": "shift"}),   # swaps with Actions
    ("darwin", "alt", {"Convert": "alt", "Tools": "cmd", "Actions": "ctrl"}),      # Option was free
    ("win32", "ctrl", {"Convert": "ctrl", "Tools": "shift", "Actions": "alt"}),
    ("linux", "alt", {"Convert": "alt", "Tools": "ctrl", "Actions": "shift"}),
    ("darwin", "bogus", {"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"}),  # junk: defaults
])
def test_migrate_trigger_keeps_the_old_key_for_convert(platform, old, want):
    got = settings.migrate_trigger(old, platform)
    assert got == want
    assert settings.validate_keys(got, platform) is None


def test_settings_migrate_old_trigger_once(prefs):
    s = prefs(trigger="alt")
    assert s.key_map()["Convert"] == "alt"
    assert settings.validate_keys(s.key_map()) is None
    assert s.q.value("trigger") is None          # the old key is gone
    again = settings.Settings(s.q)               # a second start changes nothing
    assert again.key_map() == s.key_map()


def test_migration_never_overrides_new_keys(prefs):
    keys = settings.default_keys()
    s = prefs(trigger="alt", key_convert=keys["Convert"], key_tools=keys["Tools"], key_actions=keys["Actions"])
    assert s.key_map() == keys
    assert s.q.value("trigger") is None


def test_fresh_install_gets_platform_defaults(prefs):
    assert prefs().key_map() == settings.default_keys()


def test_damaged_key_settings_fall_back_to_defaults(prefs):
    s = prefs(key_convert="shift", key_tools="shift", key_actions="shift")
    assert s.key_map() == settings.default_keys()


def test_set_wheel_key_swaps_with_the_wheel_that_had_it(prefs):
    s = prefs()
    before = s.key_map()
    assert s.set_wheel_key("Convert", before["Tools"]) == "Tools"
    after = s.key_map()
    assert after["Convert"] == before["Tools"] and after["Tools"] == before["Convert"]
    assert after["Actions"] == before["Actions"]
    assert settings.validate_keys(after) is None


def test_set_wheel_key_to_its_own_key_changes_nothing(prefs):
    s = prefs()
    before = s.key_map()
    assert s.set_wheel_key("Tools", before["Tools"]) is None
    assert s.key_map() == before


def test_set_wheel_key_rejects_a_key_this_platform_lacks(prefs):
    s = prefs()
    with pytest.raises(ValueError):
        s.set_wheel_key("Tools", "hyper")
    assert s.key_map() == settings.default_keys()


def test_every_key_in_use_can_be_moved_to_every_wheel(prefs):
    """Moving keys only among the ones already in use is what Windows and Linux always face
    (one key per wheel): every change has to be a swap, and the map must stay valid."""
    s = prefs()
    in_use = sorted(s.key_map().values())
    for wheel in settings.WHEELS:
        for key in in_use:
            s.set_wheel_key(wheel, key)
            assert s.key_map()[wheel] == key
            assert sorted(s.key_map().values()) == in_use
            assert settings.validate_keys(s.key_map()) is None


def free_key_or_skip(s, *windows):
    """A key no wheel uses. Windows and Linux offer three keys for three wheels, so there is none
    there: close `windows` and skip."""
    free = [k for k, _ in settings.key_options() if k not in s.key_map().values()]
    if not free:
        for w in windows:
            w.close()
        pytest.skip("every key is already used by a wheel on this platform")
    return free[0]


def test_set_wheel_key_saves_a_free_key(prefs):
    s = prefs()
    free = free_key_or_skip(s)
    assert s.set_wheel_key("Actions", free) is None
    assert s.key_map()["Actions"] == free
    assert s.key_label("Actions") == dict(settings.key_options())[free]


def test_settings_window_swaps_a_taken_key_and_says_so(prefs):
    s = prefs()
    w = settings.SettingsWindow(s)
    seen = []
    w.changed.connect(lambda: seen.append(True))
    old = s.key_map()
    convert = w.keys["Convert"]
    assert convert.currentData() == old["Convert"]
    convert.setCurrentIndex(convert.findData(old["Tools"]))
    assert s.key_map()["Convert"] == old["Tools"] and s.key_map()["Tools"] == old["Convert"]
    assert w.keys["Tools"].currentData() == old["Convert"]       # the other pop-up follows
    assert "Tools" in w.key_hint.text() and s.key_label("Tools") in w.key_hint.text()
    assert seen
    free = free_key_or_skip(s, w)
    w.keys["Actions"].setCurrentIndex(w.keys["Actions"].findData(free))
    assert s.key_map()["Actions"] == free
    assert w.key_hint.text() == settings.KEY_HINT
    assert not w.grab().isNull()
    w.close()


def test_welcome_names_every_wheel_key(prefs):
    w = settings.WelcomeWindow(prefs())
    from PySide6.QtWidgets import QLabel
    text = " ".join(lab.text() for lab in w.findChildren(QLabel))
    s = prefs()
    for wheel in settings.WHEELS:
        assert s.key_label(wheel) in text
    w.close()


def inside(path, folder) -> bool:
    return Path(folder).resolve() in Path(path).resolve().parents


def test_tests_never_touch_the_real_preferences(private_prefs):
    """tests/conftest.py points Settings() at a temp ini file instead of the user's store."""
    s = settings.Settings()
    assert s.q.format() == QSettings.IniFormat and inside(s.q.fileName(), private_prefs)
    real = QSettings(settings.APP, settings.APP)
    assert real.format() == QSettings.NativeFormat and real.fileName() != s.q.fileName()
    marker = f"written by the tests, pid {os.getpid()}"
    s.set("last_folder", marker)
    s.q.sync()
    assert settings.Settings().get("last_folder") == marker   # a fresh Settings() sees it...
    real.sync()
    assert real.value("last_folder") != marker                # ...and the real store doesn't
    assert inside(QSettings().fileName(), private_prefs)      # nor does a bare QSettings()


def test_tests_never_touch_the_real_login_item(private_prefs):
    if sys.platform == "darwin":
        from dragshift import platform_mac
        item = platform_mac.AGENT
    else:
        from dragshift import platform_linux
        item = platform_linux.AUTOSTART
    if not settings.IS_WIN:
        # checked before switching it on: if this failed, the next line would make a real login item
        assert inside(item, private_prefs)
    settings.set_autostart(True)
    assert settings.autostart_enabled()
    settings.set_autostart(False)
    assert not settings.autostart_enabled()


# ---------------------------------------------------------------- ring availability
def rings(paths) -> dict:
    return DragShiftApp.build_rings(None, [str(p) for p in paths])


ARCHIVE_LABELS = ["ZIP", "TAR", "TAR.GZ"]


def test_folder_gets_archive_formats_and_actions(files):
    r = rings([files["folder"]])
    assert list(r) == ["Convert", "Actions"]
    assert [i.label for i in r["Convert"]] == ARCHIVE_LABELS
    assert all(i.key.startswith("archive:") for i in r["Convert"])
    assert [i.label for i in r["Actions"]] == actions.available()
    assert all(i.key.startswith("action:") for i in r["Actions"])


def test_unsupported_file_gets_archive_formats_instead_of_unsupported(files):
    r = rings([files["odd"]])
    assert list(r) == ["Convert", "Actions"]
    assert [i.label for i in r["Convert"]] == ARCHIVE_LABELS


def test_archive_hint_counts_the_whole_selection(files):
    r = rings([files["folder"], files["odd"]])
    assert [i.hint for i in r["Convert"]] == [f"2 items → 1 {t}" for t in ARCHIVE_LABELS]


def test_folder_named_like_an_image_is_still_a_folder(tmp_path):
    d = tmp_path / "Holiday.png"
    d.mkdir()
    r = rings([d])
    assert list(r) == ["Convert", "Actions"]
    assert [i.label for i in r["Convert"]] == ARCHIVE_LABELS


def test_image_and_pdf_keep_convert_and_tools_first(files):
    for key in ("img", "pdf"):
        r = rings([files[key]])
        assert list(r) == ["Convert", "Tools", "Actions"], key
        assert r["Convert"] and r["Tools"]


def test_mixed_selection_uses_the_first_file_it_can_handle(files):
    r = rings([files["folder"], files["odd"], files["img"]])
    assert list(r) == ["Convert", "Tools", "Actions"]
    assert "PNG" not in [i.label for i in r["Convert"]]   # it's the image's ring (already PNG)


def test_action_items_have_symbols_glyphs_and_hints():
    from dragshift import design
    for name in actions.ACTIONS:
        assert name in GLYPHS and name in HINTS and name in design.SYMBOLS


def test_describe_counts_folders_as_items(files):
    d = DragShiftApp.describe(None, [str(files["folder"])])
    assert d == "Folder"
    assert DragShiftApp.describe(None, [str(files["folder"]), str(files["img"])]) == "2 items"
    assert DragShiftApp.describe(None, [str(files["img"]), str(files["pdf"])]) == "2 files"


def test_menu_opens_on_the_wanted_ring_or_the_first_available(files):
    m = RadialMenu(lambda p: rings(p), lambda p: "")
    m.wanted_ring = "Tools"
    m._load([str(files["img"])])
    assert m.ring_names[m.ring_idx] == "Tools"
    m.wanted_ring = "Tools"
    m._load([str(files["folder"])])
    assert m.ring_names == ["Convert", "Actions"] and m.ring_idx == 0
    m.deleteLater()


# ---------------------------------------------------------------- AirDrop
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_airdrop_is_hidden_off_macos(platform):
    assert "AirDrop" not in actions.available(platform)
    assert actions.available(platform) == [a for a in actions.ACTIONS if a != "AirDrop"]
    with pytest.raises(UserError):
        actions.airdrop(["/tmp/x"], platform)


def test_airdrop_is_offered_on_macos():
    assert actions.available("darwin")[0] == "AirDrop"


def test_airdrop_unavailable_is_a_readable_error(monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("macOS only")
    from dragshift import platform_mac
    monkeypatch.setattr(platform_mac, "airdrop", lambda paths: False)
    with pytest.raises(UserError, match="AirDrop"):
        actions.airdrop(["/tmp/x"], "darwin")


# ---------------------------------------------------------------- Copy
def test_copy_files_and_folders(files):
    outs = actions.copy_to([str(files["img"]), str(files["folder"])], files["dest"])
    assert outs == [files["dest"] / "Photo.png", files["dest"] / "Project"]
    assert (files["dest"] / "Project" / "sub" / "b.txt").read_text() == "b"
    assert files["img"].exists() and files["folder"].exists()     # originals untouched


def test_copy_never_overwrites(files):
    (files["dest"] / "Photo.png").write_text("mine")
    (files["dest"] / "Project").mkdir()
    outs = actions.copy_to([str(files["img"]), str(files["folder"])], files["dest"])
    assert [o.name for o in outs] == ["Photo (1).png", "Project (1)"]
    assert (files["dest"] / "Photo.png").read_text() == "mine"
    assert not any((files["dest"] / "Project").iterdir())


def test_copy_into_the_same_folder_makes_a_duplicate(files):
    out = actions.copy_to([str(files["img"])], files["src"])
    assert out == [files["src"] / "Photo (1).png"]


def test_copy_folder_into_itself_is_refused(files):
    with pytest.raises(UserError, match="into itself"):
        actions.copy_to([str(files["folder"])], files["folder"] / "sub")


def test_copy_missing_source_or_destination(files, tmp_path):
    with pytest.raises(UserError, match="moved or deleted"):
        actions.copy_to([str(tmp_path / "gone.txt")], files["dest"])
    with pytest.raises(UserError, match="doesn't exist"):
        actions.copy_to([str(files["img"])], tmp_path / "nowhere")


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_copy_without_permission_is_a_user_error(files):
    files["dest"].chmod(0o500)
    try:
        with pytest.raises(UserError, match="No permission"):
            actions.copy_to([str(files["img"])], files["dest"])
        with pytest.raises(UserError, match="No permission"):
            actions.copy_to([str(files["folder"])], files["dest"])
    finally:
        files["dest"].chmod(0o700)
    assert list(files["dest"].iterdir()) == []     # nothing half-written left behind


# ---------------------------------------------------------------- Move + Undo
def test_move_and_undo_restore_everything(files):
    record = actions.Moves()
    srcs = [files["img"], files["folder"], files["odd"]]
    outs = actions.move_to([str(p) for p in srcs], files["dest"], record)
    assert [o.name for o in outs] == ["Photo.png", "Project", "data.xyz"]
    assert not any(p.exists() for p in srcs)
    assert (files["dest"] / "Project" / "sub" / "b.txt").exists()
    assert actions.undo_move(record) == 3
    assert all(p.exists() for p in srcs)
    assert (files["folder"] / "sub" / "b.txt").read_text() == "b"
    assert list(files["dest"].iterdir()) == []
    assert actions.undo_move(record) == 0          # a second Undo does nothing


def test_move_never_overwrites(files):
    (files["dest"] / "Photo.png").write_text("mine")
    outs = actions.move_to([str(files["img"])], files["dest"])
    assert outs == [files["dest"] / "Photo (1).png"]
    assert (files["dest"] / "Photo.png").read_text() == "mine"


def test_undo_does_not_overwrite_something_new_at_the_old_path(files):
    record = actions.Moves()
    actions.move_to([str(files["img"])], files["dest"], record)
    files["img"].write_text("new file")
    assert actions.undo_move(record) == 1
    assert files["img"].read_text() == "new file"
    assert (files["src"] / "Photo (1).png").exists()


def test_undo_skips_items_deleted_since(files):
    record = actions.Moves()
    actions.move_to([str(files["img"]), str(files["odd"])], files["dest"], record)
    (files["dest"] / "data.xyz").unlink()
    assert actions.undo_move(record) == 1
    assert files["img"].exists()


def test_move_items_already_there_stay_put(files):
    with pytest.raises(UserError, match="Already in"):
        actions.move_to([str(files["img"])], files["src"])
    outs = actions.move_to([str(files["img"]), str(files["odd"])], files["dest"])
    outs2 = actions.move_to([str(o) for o in outs] + [str(files["pdf"])], files["dest"])
    assert outs2 == [files["dest"] / "Doc.pdf"]


def test_move_folder_into_itself_is_refused(files):
    with pytest.raises(UserError, match="into itself"):
        actions.move_to([str(files["folder"])], files["folder"] / "sub")
    assert files["folder"].exists()


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_move_without_permission_is_a_user_error(files):
    files["dest"].chmod(0o500)
    try:
        with pytest.raises(UserError, match="No permission"):
            actions.move_to([str(files["img"])], files["dest"])
    finally:
        files["dest"].chmod(0o700)
    assert files["img"].exists()


def test_move_failing_halfway_can_still_be_undone(files, monkeypatch):
    record = actions.Moves()
    real = actions.shutil.move
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied")
        return real(src, dst)
    monkeypatch.setattr(actions.shutil, "move", flaky)
    with pytest.raises(UserError):
        actions.move_to([str(files["img"]), str(files["odd"])], files["dest"], record)
    monkeypatch.setattr(actions.shutil, "move", real)
    assert len(record.pairs) == 1
    assert actions.undo_move(record) == 1 and files["img"].exists()


# ---------------------------------------------------------------- result cards
def test_copy_and_move_summaries(files):
    outs = actions.copy_to([str(files["img"])], files["dest"])
    r = report.summarize("action:Copy", [files["img"]], outs, "dest")
    assert r.title == "Copied “Photo.png”" and r.detail == "To dest"
    outs = actions.copy_to([str(files["pdf"]), str(files["folder"])], files["dest"])
    r = report.summarize("action:Move", [files["pdf"], files["folder"]], outs, "dest")
    assert r.title == "Moved 2 items"


def test_toast_undo_uses_the_custom_undo(files):
    stack = toasts.ToastStack()
    card = stack.new()
    calls = []

    def undo():
        calls.append(True)
        return "Moved 1 item back"
    r = report.summarize("action:Move", [files["img"]], [files["img"]], "dest")
    r.undo = undo
    card.done(r)
    card._act("Undo")
    assert calls and card.state == "info" and card.detail == "Moved 1 item back"
    assert files["img"].exists()                   # not trashed
    card.dismiss()


def test_trash_handles_folders_and_prune_false_keeps_the_destination(files, tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()

    def fake_trash(path):   # stand-in for the real Trash
        Path(path).rename(bin_ / Path(path).name)
        return True
    monkeypatch.setattr(toasts.QFile, "moveToTrash", staticmethod(fake_trash))
    outs = actions.copy_to([str(files["folder"])], files["dest"])
    assert toasts.trash(outs, prune=False) == 1
    assert files["dest"].is_dir() and (bin_ / "Project" / "sub" / "b.txt").exists()
    assert toasts.trash_message(1).startswith("Moved 1 item to the")


# ---------------------------------------------------------------- Copy Path
def test_copy_path_puts_absolute_paths_on_the_clipboard(files):
    text = actions.copy_paths([str(files["img"]), str(files["folder"])])
    assert text == f"{os.path.abspath(files['img'])}\n{os.path.abspath(files['folder'])}"
    assert QGuiApplication.clipboard().text() == text


def test_path_text_makes_relative_paths_absolute(files, monkeypatch):
    monkeypatch.chdir(files["src"])
    assert actions.path_text(["Photo.png"]) == os.path.join(os.getcwd(), "Photo.png")


# ---------------------------------------------------------------- Terminal
def test_terminal_dir_is_the_folder_or_the_files_folder(files):
    assert actions.terminal_dir([str(files["folder"])]) == files["folder"]
    assert actions.terminal_dir([str(files["img"]), str(files["folder"])]) == files["src"]


def test_terminal_on_macos_opens_terminal_app():
    launch = actions.terminal_command("/Users/me/a b;&c", "darwin", which=lambda n: None)
    assert launch.args == ["open", "-a", "Terminal", "/Users/me/a b;&c"]


def test_terminal_on_windows_prefers_windows_terminal_and_passes_the_folder_as_cwd():
    launch = actions.terminal_command(r"C:\a & b", "win32", which=lambda n: r"C:\wt.exe" if n == "wt" else None)
    assert launch.args == [r"C:\wt.exe", "-d", "."] and launch.cwd == r"C:\a & b"
    launch = actions.terminal_command(r"C:\a & b", "win32", which=lambda n: None)
    assert launch.args[-1] == "/K" and launch.cwd == r"C:\a & b"
    assert launch.flags == actions.CREATE_NEW_CONSOLE
    assert all("a & b" not in a for a in launch.args)   # never on a command line cmd.exe parses


def test_terminal_on_linux_uses_the_first_terminal_found():
    found = {"gnome-terminal": "/usr/bin/gnome-terminal", "xterm": "/usr/bin/xterm"}
    launch = actions.terminal_command("/home/me/x", "linux", which=found.get)
    assert launch.args == ["/usr/bin/gnome-terminal"] and launch.cwd == "/home/me/x"
    launch = actions.terminal_command("/home/me/x", "linux", which={"x-terminal-emulator": "/usr/bin/x-t"}.get)
    assert launch.args == ["/usr/bin/x-t"]
    with pytest.raises(UserError, match="No terminal"):
        actions.terminal_command("/home/me/x", "linux", which=lambda n: None)


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_open_terminal_runs_an_argument_list_without_a_shell(files, platform):
    calls = []
    actions.open_terminal([str(files["img"])], platform, which=lambda n: f"/bin/{n}",
                          popen=lambda args, **kw: calls.append((args, kw)))
    (args, kw), = calls
    assert isinstance(args, list) and all(isinstance(a, str) for a in args)
    assert "shell" not in kw
    if platform == "darwin":
        assert args[-1] == str(files["src"])
    else:
        assert kw["cwd"] == str(files["src"])
    assert ("creationflags" in kw) == (platform == "win32")


def test_open_terminal_failure_is_a_user_error(files):
    def boom(args, **kw):
        raise FileNotFoundError(2, "No such file")
    with pytest.raises(UserError, match="terminal"):
        actions.open_terminal([str(files["img"])], "darwin", popen=boom)


# ---------------------------------------------------------------- Float
def test_float_panel_paints_and_names_itself(files):
    p = floating.FloatPanel([str(files["img"])])
    assert p.title == "Photo.png" and "Photo.png" in p.accessibleName()
    assert not p.thumb.isNull() and p.is_picture
    p.hovered = True
    assert not p.grab().isNull()
    p.close()


def test_float_panel_for_a_folder_and_many_items(files):
    p = floating.FloatPanel([str(files["folder"]), str(files["odd"]), str(files["img"])])
    assert p.title == "3 items" and not p.is_picture and not p.thumb.isNull()   # folder icon
    assert not p.grab().isNull()
    p.close()


def test_float_drag_offers_copy_with_file_urls(files, monkeypatch):
    seen = {}

    def fake_exec(drag, supported, default=None):
        seen["urls"] = [u.toLocalFile() for u in drag.mimeData().urls()]
        seen["supported"], seen["default"] = supported, default
        return Qt.CopyAction
    monkeypatch.setattr(floating.QDrag, "exec", fake_exec)
    p = floating.FloatPanel([str(files["img"]), str(files["folder"])])
    assert p.start_drag() is not None
    assert seen["urls"] == [str(files["img"]), str(files["folder"])]
    assert seen["supported"] == Qt.CopyAction                # never a move
    assert files["img"].exists() and files["folder"].exists()
    p.close()


def test_float_drag_skips_missing_files(files, monkeypatch):
    calls = []
    monkeypatch.setattr(floating.QDrag, "exec", lambda *a, **k: calls.append(a) or Qt.CopyAction)
    p = floating.FloatPanel([str(files["img"])])
    files["img"].unlink()
    assert p.missing() and p.start_drag() is None and not calls
    assert not p.grab().isNull()    # paints the "Moved or deleted" state
    p.close()


def test_float_manager_allows_many_and_remembers_positions(files):
    floats = floating.Floats()
    a = floats.open([str(files["img"])], QPoint(300, 300))
    b = floats.open([str(files["pdf"])], QPoint(300, 300))
    assert len(floats.panels) == 2 and a.pos() != b.pos()     # cascaded, not stacked
    a.move(a.pos() + QPoint(40, 10))
    moved_to = a.pos()
    a.close()
    assert floats.panels == [b]
    again = floats.open([str(files["img"])], QPoint(0, 0))
    assert again.pos() == moved_to                            # same place this session
    for w in list(floats.panels):
        w.close()
    assert floats.panels == []


def test_float_escape_closes(files):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    floats = floating.Floats()
    p = floats.open([str(files["img"])], QPoint(100, 100))
    p.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert floats.panels == []


# ---------------------------------------------------------------- the watcher: key -> wheel
class FakeInput:
    """Scripted input: each tick reads (left button, keys down, escape, cursor x)."""

    def __init__(self, watcher, monkeypatch):
        self.w = watcher
        self.state = (False, set(), False)
        self.x = 0
        watcher._key_down = lambda name: name in self.state[1]
        watcher._state = lambda: (self.state[0], {wh for wh, k in watcher.keys.items() if k in self.state[1]},
                                  self.state[2])
        watcher._nav_state = lambda: set()   # never read the real keyboard in tests
        monkeypatch.setattr(trigger.QCursor, "pos", staticmethod(lambda: QPoint(self.x, 0)))

    def tick(self, lbtn, keys=(), esc=False, x=None):
        self.state = (lbtn, set(keys), esc)
        if x is not None:
            self.x = x
        self.w._tick()


@pytest.fixture
def watch(monkeypatch):
    menu = {"visible": False, "got_drag": False}
    w = trigger.DragShiftWatcher(lambda: menu["visible"], lambda: menu["got_drag"])
    w.set_keys({"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"})
    events = []
    w.show_menu.connect(lambda pos, ring: events.append(("show", ring)))
    w.switch_ring.connect(lambda ring: events.append(("switch", ring)))
    return w, FakeInput(w, monkeypatch), menu, events


@pytest.mark.parametrize("key,wheel", [("shift", "Convert"), ("cmd", "Tools"), ("ctrl", "Actions")])
def test_each_key_opens_its_own_wheel(watch, key, wheel):
    w, inp, menu, events = watch
    inp.tick(True, x=0)
    inp.tick(True, x=20)                 # dragging, no key yet: nothing
    assert events == []
    inp.tick(True, [key], x=24)
    assert events == [("show", wheel)]


def test_key_held_before_the_drag_moves_far_enough_still_opens(watch):
    w, inp, menu, events = watch
    inp.tick(True, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=3)
    assert events == []
    inp.tick(True, ["cmd"], x=15)
    assert events == [("show", "Tools")]
    inp.tick(True, ["cmd"], x=30)        # fires once per drag
    assert events == [("show", "Tools")]


def test_key_held_before_the_button_goes_down_is_ignored(watch):
    """A normal Command-drag in Finder must not pop the Tools wheel."""
    w, inp, menu, events = watch
    inp.tick(False, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=40)
    inp.tick(True, ["cmd"], x=80)
    assert events == []


def test_stale_key_counts_once_released_and_pressed_again(watch):
    w, inp, menu, events = watch
    inp.tick(False, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=40)
    inp.tick(True, [], x=50)
    assert events == []
    inp.tick(True, ["cmd"], x=60)
    assert events == [("show", "Tools")]


def test_other_key_opens_while_a_stale_one_is_held(watch):
    w, inp, menu, events = watch
    inp.tick(False, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=40)
    inp.tick(True, ["cmd", "shift"], x=45)
    assert events == [("show", "Convert")]


def test_stale_keys_reset_between_drags(watch):
    w, inp, menu, events = watch
    inp.tick(False, ["cmd"], x=0)
    inp.tick(True, ["cmd"], x=0)
    inp.tick(False, [], x=0)              # button up, key up
    inp.tick(True, [], x=0)               # next drag
    inp.tick(True, ["cmd"], x=40)
    assert events == [("show", "Tools")]


def test_key_and_button_on_the_same_tick_counts(watch):
    """Polling every 15 ms can see both go down together; that's a deliberate press."""
    w, inp, menu, events = watch
    inp.tick(False, [], x=0)
    inp.tick(True, ["shift"], x=0)
    inp.tick(True, ["shift"], x=20)
    assert events == [("show", "Convert")]


def test_newly_pressed_key_beats_one_already_held(watch):
    w, inp, menu, events = watch
    w.enabled = False
    inp.tick(True, ["shift"], x=0)
    inp.tick(True, ["shift"], x=20)
    w.enabled = True
    inp.tick(True, ["shift", "ctrl"], x=22)
    assert events == [("show", "Actions")]


def test_other_keys_switch_wheels_while_open(watch):
    w, inp, menu, events = watch
    inp.tick(True, x=0)
    inp.tick(True, ["shift"], x=20)
    menu["visible"] = menu["got_drag"] = True
    inp.tick(True, [], x=21)                    # Shift released: wheel stays open
    inp.tick(True, ["ctrl"], x=22)
    inp.tick(True, ["ctrl"], x=23)              # held: no repeat
    inp.tick(True, [], x=24)
    inp.tick(True, ["cmd"], x=25)
    inp.tick(True, [], x=26)
    inp.tick(True, ["shift"], x=27)
    assert events == [("show", "Convert"), ("switch", "Actions"), ("switch", "Tools"), ("switch", "Convert")]


def test_no_switch_before_the_file_arrives(watch):
    w, inp, menu, events = watch
    menu["visible"] = True
    inp.tick(True, ["cmd"], x=0)
    assert events == []


def test_disabled_watcher_never_opens(watch):
    w, inp, menu, events = watch
    w.enabled = False
    inp.tick(True, x=0)
    inp.tick(True, ["shift"], x=50)
    assert events == []


def test_release_hides_the_menu(watch, qapp):
    w, inp, menu, events = watch
    hidden = []
    w.hide_menu.connect(lambda: hidden.append(True))
    menu["visible"] = menu["got_drag"] = True
    inp.tick(False)
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(200, loop.quit)
    loop.exec()
    assert hidden


def test_watcher_reads_each_mapped_key(monkeypatch):
    """_state asks the platform about every wheel's key, not just one."""
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)
    w.set_keys({"Convert": "shift", "Tools": "cmd", "Actions": "ctrl"})
    asked = []

    class Plat:
        @staticmethod
        def key_down(name):
            asked.append(name)
            return name == "ctrl"
        left_button_down = staticmethod(lambda: True)
        escape_down = staticmethod(lambda: False)
    w._mac = Plat
    assert w._state() == (True, {"Actions"}, False)
    assert sorted(asked) == ["cmd", "ctrl", "shift"]


def test_platform_modules_know_the_command_key():
    from dragshift import platform_linux, platform_mac
    assert platform_mac.KEY_FLAGS["cmd"] == platform_mac.COMMAND
    assert "cmd" in platform_linux.KEY_MASKS
    assert trigger.KEY_VK["ctrl"] == (0x11,) and trigger.KEY_VK["alt"] == (0x12,)
