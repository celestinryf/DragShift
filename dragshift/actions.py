"""The Actions wheel: things to do with any dragged item, files and folders alike.

Copy and Move never overwrite (a clash becomes "name (1)"), Move can be undone, and the
Terminal launchers take argument lists with the folder passed as the working directory, so
file names are never parsed by a shell."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .util import UserError, unique_path

ACTIONS = ["AirDrop", "Float", "Copy", "Move", "Copy Path", "Terminal", "Zip"]
# for "Couldn't …" error titles
VERBS = {"AirDrop": "AirDrop", "Float": "float", "Copy": "copy", "Move": "move", "Copy Path": "copy the path",
         "Terminal": "open Terminal", "Zip": "zip"}


def available(platform: str = sys.platform) -> list[str]:
    """Actions offered on this platform. AirDrop only exists on macOS."""
    return [a for a in ACTIONS if a != "AirDrop" or platform == "darwin"]


# ---------------------------------------------------------------- copy / move
def _check_destination(dest: str | Path) -> Path:
    d = Path(dest)
    if not d.is_dir():
        raise UserError(f"The folder “{d.name or d}” doesn't exist any more.")
    return d


def _check_source(src: Path, dest: Path, verb: str):
    if not src.exists() and not src.is_symlink():
        raise UserError(f"“{src.name}” was moved or deleted.")
    if src.is_dir() and not src.is_symlink():
        try:
            dest.resolve().relative_to(src.resolve())
        except ValueError:
            return
        raise UserError(f"Can't {verb} the folder “{src.name}” into itself.")


def _permission(e: OSError, verb: str, name: str, dest: Path) -> UserError:
    if isinstance(e, PermissionError):
        return UserError(f"No permission to {verb} “{name}” to “{dest.name or dest}”.")
    return UserError(f"Couldn't {verb} “{name}”: {e.strerror or e}.")


def copy_to(paths: list[str], dest: str | Path) -> list[Path]:
    """Copy files and folders into `dest`. Returns the new paths, in order."""
    d = _check_destination(dest)
    srcs = [Path(p) for p in paths]
    for src in srcs:
        _check_source(src, d, "copy")
    outs: list[Path] = []
    for src in srcs:
        target = unique_path(d / src.name)
        try:
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, target, symlinks=True)
            else:
                shutil.copy2(src, target, follow_symlinks=False)
        except OSError as e:
            _remove_partial(target)
            raise _permission(e, "copy", src.name, d) from e
        outs.append(target)
    return outs


def _remove_partial(target: Path):
    """Clean up a half-written copy (only ever a path this copy just created)."""
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    except OSError:
        pass


@dataclass
class Moves:
    """What a Move did, so Undo can put everything back."""
    pairs: list[tuple[Path, Path]] = field(default_factory=list)   # (original, new)


def move_to(paths: list[str], dest: str | Path, record: Moves | None = None) -> list[Path]:
    """Move files and folders into `dest`. Items already in `dest` stay where they are.
    Each finished move is added to `record` straight away, so a failure halfway can still
    be undone. Returns the new paths."""
    d = _check_destination(dest)
    record = record if record is not None else Moves()
    srcs = [Path(p) for p in paths]
    for src in srcs:
        _check_source(src, d, "move")
    todo = [s for s in srcs if s.parent.resolve() != d.resolve()]
    if not todo:
        raise UserError(f"Already in “{d.name or d}”.")
    for src in todo:
        target = unique_path(d / src.name)
        try:
            shutil.move(str(src), str(target))
        except OSError as e:
            raise _permission(e, "move", src.name, d) from e
        record.pairs.append((src, target))
    return [new for _, new in record.pairs]


def undo_move(record: Moves) -> int:
    """Move everything back. When something new now sits at an original path, the item
    goes back beside it as "name (1)" instead of overwriting it. Returns how many moved."""
    n = 0
    for original, new in reversed(record.pairs):
        if not new.exists() and not new.is_symlink():
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new), str(unique_path(original)))
            n += 1
        except OSError:
            continue
    record.pairs.clear()
    return n


# ---------------------------------------------------------------- clipboard
def path_text(paths: list[str]) -> str:
    """Absolute paths, one per line (POSIX paths on macOS and Linux)."""
    return "\n".join(os.path.abspath(p) for p in paths)


def copy_paths(paths: list[str]) -> str:
    from PySide6.QtGui import QGuiApplication
    text = path_text(paths)
    QGuiApplication.clipboard().setText(text)
    return text


# ---------------------------------------------------------------- terminal
@dataclass
class Launch:
    args: list[str]
    cwd: str | None = None
    flags: int = 0       # Windows creationflags


CREATE_NEW_CONSOLE = 0x00000010
LINUX_TERMINALS = ["x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "mate-terminal",
                   "tilix", "kitty", "alacritty", "xterm"]


def terminal_dir(paths: list[str]) -> Path:
    """A folder opens as itself; a file opens in the folder that holds it."""
    p = Path(os.path.abspath(paths[0]))
    return p if p.is_dir() else p.parent


def terminal_command(directory: str | Path, platform: str = sys.platform, which=shutil.which) -> Launch:
    d = str(directory)
    if platform == "darwin":
        return Launch(["open", "-a", "Terminal", d])
    if platform == "win32":
        # the folder goes in as the working directory: cmd.exe and wt.exe would parse it
        # ("&", ";") if it were part of the command line
        wt = which("wt")
        if wt:
            return Launch([wt, "-d", "."], cwd=d)
        return Launch([os.environ.get("COMSPEC") or "cmd.exe", "/K"], cwd=d, flags=CREATE_NEW_CONSOLE)
    for name in LINUX_TERMINALS:
        exe = which(name)
        if exe:
            return Launch([exe], cwd=d)
    raise UserError("No terminal app was found. Install one, such as GNOME Terminal or xterm.")


def open_terminal(paths: list[str], platform: str = sys.platform, which=shutil.which, popen=subprocess.Popen):
    launch = terminal_command(terminal_dir(paths), platform, which)
    kwargs = {"cwd": launch.cwd}
    if platform == "win32":
        kwargs["creationflags"] = launch.flags   # only Windows accepts creationflags
    try:
        popen(launch.args, **kwargs)
    except OSError as e:
        raise UserError(f"Couldn't open the terminal: {e.strerror or e}.") from e


# ---------------------------------------------------------------- AirDrop
def airdrop(paths: list[str], platform: str = sys.platform):
    if platform != "darwin":
        raise UserError("AirDrop is only available on a Mac.")
    from . import platform_mac
    if not platform_mac.airdrop(paths):
        raise UserError("AirDrop isn't available. Check that Wi-Fi and Bluetooth are on.")
