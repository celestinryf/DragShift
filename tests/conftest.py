"""Shared test setup: the suite never reads or writes the user's real DragShift preferences or
login item, whichever test file runs first."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

from dragshift import platform_linux, settings  # noqa: E402


class FakeLogin:
    """Stands in for the Windows Run registry key, which has no file to redirect."""

    def __init__(self):
        self.on = False

    def enabled(self) -> bool:
        return self.on

    def set(self, on: bool):
        self.on = bool(on)


@pytest.fixture(scope="session", autouse=True)
def private_prefs(tmp_path_factory):
    """Points Settings() and the login item at a temp folder for the whole run.

    QSettings(org, app) always uses the native store (a plist written through cfprefsd on macOS,
    the registry on Windows), and setPath can't move that store, so Settings() gets an ini file
    here through `settings.user_store`. Any bare QSettings() lands here too, via the default
    format and path."""
    d = tmp_path_factory.mktemp("prefs")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    for scope in (QSettings.UserScope, QSettings.SystemScope):
        QSettings.setPath(QSettings.IniFormat, scope, str(d))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "user_store", lambda: QSettings(str(d / "DragShift.ini"), QSettings.IniFormat))
        mp.setattr(platform_linux, "AUTOSTART", d / "autostart" / "dragshift.desktop")
        if sys.platform == "darwin":
            from dragshift import platform_mac
            mp.setattr(platform_mac, "AGENT", d / "LaunchAgents" / "com.dragshift.app.plist")
        if settings.IS_WIN:
            login = FakeLogin()
            mp.setattr(settings, "autostart_enabled", login.enabled)
            mp.setattr(settings, "set_autostart", login.set)
        yield d
    QSettings.setDefaultFormat(QSettings.NativeFormat)
