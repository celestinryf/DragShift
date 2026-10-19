"""Archives: make ZIP, TAR and TAR.GZ from any selection (files, folders, both), and extract
or convert ZIP, TAR (plain, gz, bz2, xz), single gz/bz2/xz files and RAR (read only).

Names inside an archive come from whoever made it, so extraction checks every entry before
anything is written: no absolute paths, drive letters or "..", links must point inside the new
folder, and caps on size and count stop zip bombs. Everything is written into a hidden
temporary folder that only becomes the real one when it all worked, and links are made last,
so no file is ever written through one.

RAR is read through libarchive's `bsdtar` (built into macOS and Windows 10+, `libarchive-tools`
on Linux): it re-packs the RAR as a tar stream on stdout (`@archive` input), and that stream goes
through the same checks as any tar. Nothing of unrar's (non-free) code is used."""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from .convert import atomic_save, temp_path
from .media import _quiet
from .util import UserError, output_dir, readable_name_errors, unique_path

CREATE_FORMATS = {"ZIP": ".zip", "TAR": ".tar", "TAR.GZ": ".tar.gz"}
TARGETS = list(CREATE_FORMATS)
# longest first: "a.tar.gz" is a TAR.GZ, not a GZ
SUFFIXES = {".tar.gz": "TAR.GZ", ".tgz": "TAR.GZ", ".tar.bz2": "TAR.BZ2", ".tbz2": "TAR.BZ2",
            ".tbz": "TAR.BZ2", ".tar.xz": "TAR.XZ", ".txz": "TAR.XZ", ".tar": "TAR", ".zip": "ZIP",
            ".rar": "RAR", ".gz": "GZ", ".bz2": "BZ2", ".xz": "XZ"}
SINGLE = {"GZ": gzip.open, "BZ2": bz2.open, "XZ": lzma.open}   # one compressed file, no folder inside

# Zip bomb guards. A real backup can be big, so the size cap is generous; the free disk space
# check below catches the rest. Tests lower these.
MAX_ENTRIES = 200_000
MAX_TOTAL = 32 * 1024 ** 3        # bytes, all files together once extracted
MAX_LINK = 4096                   # bytes of a symlink's target (PATH_MAX)
CHUNK = 1024 * 1024

JUNK_NAMES = {".DS_Store", "__MACOSX"}      # Finder's folder settings and its zip resource forks
DRIVE = re.compile(r"^[A-Za-z]:")
ENCRYPTED = ("“{name}” is password-protected, and DragShift can't open encrypted archives. "
             "Open it in an app that asks for the password.")


# ---------------------------------------------------------------- names and formats
def archive_suffix(path: str | Path) -> str:
    """'.tar.gz' for 'a.tar.gz', '.zip' for 'a.zip', '' for anything else."""
    low = Path(path).name.lower()
    return next((s for s in sorted(SUFFIXES, key=len, reverse=True) if low.endswith(s) and len(low) > len(s)), "")


def format_label(path: str | Path) -> str:
    """'TAR.GZ', 'ZIP', 'RAR'… for an archive's file name."""
    return SUFFIXES.get(archive_suffix(path), Path(path).suffix.lstrip(".").upper())


def base_name(path: str | Path) -> str:
    """'Photos' for 'Photos.tar.gz'."""
    name = Path(path).name
    return name[: len(name) - len(archive_suffix(path))] or name


def targets_for(src_ext: str) -> list[str]:
    """Archive formats an archive can become (`src_ext` may be '.tar.gz')."""
    own = SUFFIXES.get("." + src_ext.lower().lstrip("."))
    return [t for t in TARGETS if t != own]


def unique_name(folder: Path, stem: str, ext: str = "") -> Path:
    """`folder/stem.ext`, or 'stem (1).ext'… if taken. Unlike util.unique_path this keeps a
    double extension whole ('Archive (1).tar.gz', not 'Archive.tar (1).gz')."""
    cand = folder / f"{stem}{ext}"
    i = 1
    with readable_name_errors():
        while cand.exists() or cand.is_symlink():
            cand = folder / f"{stem} ({i}){ext}"
            i += 1
    return cand


def _is_junk(name: str) -> bool:
    """.DS_Store, anything under __MACOSX/, and AppleDouble resource forks ('._photo.jpg')."""
    return name in JUNK_NAMES or name.startswith("._")


def _hooks():
    from . import media
    return media.hooks()


class _Progress:
    """Reports bytes done to the job's progress card and stops when the card is closed."""

    def __init__(self, total: int):
        self.total, self.done, self.last = max(1, total), 0, 0.0
        self.report, self.cancel = _hooks()

    def add(self, n: int):
        self.done += n
        if self.cancel is not None and self.cancel.is_set():
            from .media import Cancelled
            raise Cancelled("Cancelled.")
        frac = min(1.0, self.done / self.total)
        if self.report is not None and frac - self.last >= 0.01:
            self.last = frac
            self.report(frac)


# ---------------------------------------------------------------- create
@dataclass
class Item:
    path: Path
    arcname: str
    kind: str          # "file" | "dir" | "link"


def collect(paths: list[str | Path], skip: set[Path] = frozenset()) -> list[Item]:
    """Everything to archive, in a fixed order: each selected item under its own name (a clash
    between two selected 'a.txt' becomes 'a (1).txt'), folders walked in sorted order with their
    empty folders kept. Symlinks are stored as links, never followed. Junk inside folders is
    skipped; something junk-like the user picked on purpose is kept."""
    items: list[Item] = []
    taken: set[str] = set()
    for src in (Path(p) for p in paths):
        if not src.exists() and not src.is_symlink():
            raise UserError(f"“{src.name}” was moved or deleted.")
        top = _unique_arcname(src.name, taken)
        if src.is_symlink():
            items.append(Item(src, top, "link"))
        elif src.is_dir():
            items.append(Item(src, top, "dir"))
            items += _walk(src, top, skip)
        else:
            items.append(Item(src, top, "file"))
    return items


def _unique_arcname(name: str, taken: set[str]) -> str:
    stem, ext = os.path.splitext(name)
    cand, i = name, 1
    while cand.lower() in taken:
        cand = f"{stem} ({i}){ext}"
        i += 1
    taken.add(cand.lower())
    return cand


def _walk(folder: Path, top: str, skip: set[Path]) -> list[Item]:
    items: list[Item] = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        rel = Path(root).relative_to(folder).as_posix()
        prefix = top if rel == "." else f"{top}/{rel}"
        dirs[:] = sorted(d for d in dirs if not _is_junk(d))
        for d in list(dirs):
            p = Path(root, d)
            if p.is_symlink():          # os.walk lists a linked folder but never enters it
                items.append(Item(p, f"{prefix}/{d}", "link"))
                dirs.remove(d)
            else:
                items.append(Item(p, f"{prefix}/{d}", "dir"))
        for f in sorted(files):
            p = Path(root, f)
            if _is_junk(f) or p in skip:
                continue
            items.append(Item(p, f"{prefix}/{f}", "link" if p.is_symlink() else "file"))
    # by path parts: each folder comes right before its contents, everything in name order
    items.sort(key=lambda it: it.arcname.split("/"))
    return items


def _zip_time(ts: float) -> tuple:
    # ZIP can't store dates before 1980
    return time.localtime(max(ts, 315532800 + 86400))[:6]


def _write_zip(dst: Path, items: list[Item], progress: _Progress):
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as z:
        for it in items:
            if it.kind == "link":
                info = zipfile.ZipInfo(it.arcname, _zip_time(it.path.lstat().st_mtime))
                info.create_system = 3            # Unix: the mode below marks it as a link
                info.external_attr = (stat.S_IFLNK | 0o755) << 16
                z.writestr(info, os.readlink(it.path), zipfile.ZIP_STORED)
            elif it.kind == "dir":
                z.write(it.path, it.arcname + "/")
            else:
                z.write(it.path, it.arcname)
                progress.add(it.path.stat().st_size)


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Don't put the user's account name and ids into the archive."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _write_tar(dst: Path, items: list[Item], progress: _Progress, mode: str):
    with tarfile.open(dst, mode, format=tarfile.PAX_FORMAT) as t:
        for it in items:
            # recursive=False: _walk already lists folder contents; tarfile stores links as links
            t.add(it.path, it.arcname, recursive=False, filter=_anonymous)
            if it.kind == "file":
                progress.add(it.path.stat().st_size)


def write_archive(dst: Path, items: list[Item], fmt: str):
    progress = _Progress(sum(it.path.stat().st_size for it in items if it.kind == "file"))
    if fmt == "ZIP":
        _write_zip(dst, items, progress)
    else:
        _write_tar(dst, items, progress, "w:gz" if fmt == "TAR.GZ" else "w")


def create(paths: list[str | Path], fmt: str) -> Path:
    """One archive of the whole selection, beside the first item: 'Photos.zip' for one item,
    'Archive.zip' for several."""
    fmt = fmt.upper()
    if fmt not in CREATE_FORMATS:
        raise ValueError(f"Can't make a {fmt} archive")
    srcs = [Path(p) for p in paths]
    stem = srcs[0].name if len(srcs) == 1 else "Archive"
    out = unique_name(output_dir(srcs[0]), stem, CREATE_FORMATS[fmt])
    # the hidden temp file may land inside a selected folder: never archive it into itself
    tmp = temp_path(out)
    items = collect(srcs, {out, tmp})
    atomic_save(lambda dst: write_archive(Path(dst), items, fmt), out, tmp)
    return out


# ---------------------------------------------------------------- read
@dataclass
class Entry:
    raw: str                     # name as stored
    kind: str                    # "file" | "dir" | "link" | "hardlink" | "other"
    size: int = 0                # -1 when unknown (single .gz)
    link: str = ""
    mode: int = 0
    mtime: float | None = None
    open: Callable[[], BinaryIO] | None = None


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Names without the UTF-8 flag are officially cp437 (what zipfile decodes them as), but
    macOS and Linux tools often write UTF-8 without setting the flag: try that first."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return info.orig_filename


def _zip_mtime(date_time: tuple) -> float | None:
    try:
        return time.mktime(date_time + (0, 0, -1))
    except (OverflowError, ValueError):   # a damaged or zeroed date
        return None


def _zip_entries(z: zipfile.ZipFile, src: Path) -> list[Entry]:
    entries = []
    infos = z.infolist()
    _check_count(len(infos))
    for info in infos:
        # bit 0: classic ZipCrypto; method 99: WinZip AES
        if info.flag_bits & 0x1 or info.compress_type == 99:
            raise UserError(ENCRYPTED.format(name=src.name))
        mode = (info.external_attr >> 16) if info.create_system == 3 else 0
        mtime = _zip_mtime(info.date_time)
        name = _zip_name(info)
        if stat.S_ISLNK(mode):
            if info.file_size > MAX_LINK:
                raise UserError(f"“{src.name}” has a damaged link entry, so DragShift didn't extract it.")
            target = z.read(info).decode("utf-8", "replace")
            entries.append(Entry(name, "link", 0, target, mode, mtime))
        elif info.is_dir():
            entries.append(Entry(name, "dir", 0, "", mode, mtime))
        else:
            entries.append(Entry(name, "file", info.file_size, "", mode, mtime,
                                 lambda i=info: z.open(i)))
    return entries


def _tar_entry(t: tarfile.TarFile, m: tarfile.TarInfo) -> Entry:
    if m.isreg():
        return Entry(m.name, "file", m.size, "", m.mode, m.mtime, lambda: t.extractfile(m))
    if m.isdir():
        return Entry(m.name, "dir", 0, "", m.mode, m.mtime)
    if m.issym():
        return Entry(m.name, "link", 0, m.linkname, m.mode, m.mtime)
    if m.islnk():
        return Entry(m.name, "hardlink", 0, m.linkname, m.mode, m.mtime)
    return Entry(m.name, "other")      # devices, FIFOs: never extracted


def _check_count(n: int):
    if n > MAX_ENTRIES:
        raise UserError(f"This archive holds more than {MAX_ENTRIES:,} items, which looks like a "
                        f"zip bomb, so DragShift didn't extract it.")


def bsdtar() -> str | None:
    """libarchive's bsdtar: built into macOS, and Windows 10+ ships it as tar.exe."""
    if sys.platform == "darwin" and os.path.exists("/usr/bin/bsdtar"):
        return "/usr/bin/bsdtar"
    if sys.platform == "win32":
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
        if exe.exists():
            return str(exe)
    return shutil.which("bsdtar")


def _rar_stream(src: Path, tool: str) -> Iterator[Entry]:
    """The RAR's entries, re-packed by bsdtar as a tar stream. The path goes in as one
    argument ('@' + absolute path, so it can't read as an option); no shell."""
    # stderr goes to a file: a full stderr pipe would stall bsdtar while we read stdout
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen([tool, "-c", "-f", "-", "--format", "pax", "@" + str(src.resolve())],
                            stdout=subprocess.PIPE, stderr=errors, **_quiet())
    try:
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as t:
                n = 0
                for m in t:
                    n += 1
                    _check_count(n)
                    yield _tar_entry(t, m)
        except tarfile.ReadError:
            pass          # bsdtar failed before writing a tar: its message is explained below
        proc.stdout.close()
        failed = proc.wait() != 0
        errors.seek(0)
        err = errors.read(16384).decode("utf-8", "replace").lower()
        if failed:
            if "passphrase" in err or "encrypt" in err or "password" in err:
                raise UserError(ENCRYPTED.format(name=src.name))
            raise UserError(f"“{src.name}” is damaged, or it's a RAR version DragShift can't read.")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        errors.close()


def kind(src: Path) -> str:
    label = format_label(src)
    if label == "ZIP":
        return "zip"
    if label == "RAR":
        return "rar"
    if label in SINGLE:
        return "single"
    if label.startswith("TAR"):
        return "tar"
    raise UserError(f"“{src.name}” isn't an archive DragShift can open.")


@contextmanager
def _reader(src: Path):
    """Yields a function returning a fresh iterator of the archive's entries. ZIP and TAR read
    their index once (and their entries open lazily from it); RAR is a stream, decoded again
    each time it's walked."""
    k = kind(src)
    try:
        if k == "zip":
            with zipfile.ZipFile(src) as z:
                entries = _zip_entries(z, src)
                yield lambda: iter(entries)
        elif k == "tar":
            with tarfile.open(src, "r:*") as t:
                members = []
                for m in t:
                    members.append(m)
                    _check_count(len(members))
                entries = [_tar_entry(t, m) for m in members]
                yield lambda: iter(entries)
        elif k == "rar":
            tool = bsdtar()
            if tool is None:
                raise UserError("Opening RAR files needs bsdtar (libarchive), and DragShift couldn't "
                                "find it. On Linux, install the libarchive-tools package.")
            yield lambda: _rar_stream(src, tool)
        else:
            opener = SINGLE[format_label(src)]
            entry = Entry(base_name(src), "file", -1, "", 0o644, src.stat().st_mtime, lambda: opener(src))
            yield lambda: iter([entry])
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, zlib.error, lzma.LZMAError, gzip.BadGzipFile) as e:
        raise UserError(f"“{src.name}” is damaged or isn't really a {format_label(src)} file.") from e
    except NotImplementedError as e:      # a zip method Python lacks (e.g. deflate64)
        raise UserError(f"“{src.name}” uses a compression method DragShift can't read.") from e


# ---------------------------------------------------------------- checks
class Unsafe(UserError):
    pass


def _unsafe(src: Path, name: str) -> Unsafe:
    return Unsafe(f"“{src.name}” tries to write outside its folder (“{name}”), so DragShift "
                  f"didn't extract it.")


def clean_name(raw: str, src: Path) -> str | None:
    """A safe relative path ('a/b.txt') for an entry name, None for entries to skip (junk,
    the root itself). Raises Unsafe for absolute paths, drive letters, '..' and NUL bytes."""
    if "\x00" in raw:
        raise _unsafe(src, raw.replace("\x00", ""))
    name = raw.replace("\\", "/")     # a Windows-made zip's separator, and a traversal trick
    if name.startswith("/") or DRIVE.match(name):
        raise _unsafe(src, raw)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise _unsafe(src, raw)
    if sys.platform == "win32" and any(":" in p for p in parts):   # NTFS alternate data streams
        raise _unsafe(src, raw)
    if not parts or any(_is_junk(p) for p in parts):
        return None
    return "/".join(parts)


def _link_ok(name: str, target: str) -> bool:
    """A link at `name` pointing to `target` stays inside the folder (checked on the names;
    the real paths are checked again after the link is made)."""
    t = target.replace("\\", "/")
    if not t or "\x00" in t or t.startswith("/") or DRIVE.match(t):
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(name), t))
    return joined != ".." and not joined.startswith("../")


@dataclass
class Plan:
    total: int = 0       # bytes once extracted (known sizes only)
    prefix: str = ""     # one folder everything sits in, dropped when extracting


def _strip(name: str, prefix: str) -> str:
    return name[len(prefix) + 1:] if prefix and name.startswith(prefix + "/") else name


def plan(entries: Iterator[Entry], src: Path, strip: bool) -> Plan:
    """Check every entry before anything is written."""
    names: list[tuple[str, Entry]] = []
    sizes: dict[str, int] = {}     # file or hard link name -> bytes it extracts to
    total = 0
    for e in entries:
        name = clean_name(e.raw, src)
        if name is None or e.kind == "other":
            continue
        if e.kind == "hardlink":
            link = clean_name(e.link, src)
            if link is None:
                raise _unsafe(src, e.link)
            # a hard link is extracted as a full copy, so it costs as much as what it names
            sizes[name] = sizes.get(link, 0)
            total += sizes[name]
        if e.kind == "file":
            sizes[name] = max(0, e.size)
        total += max(0, e.size)
        if total > MAX_TOTAL:
            raise UserError(f"“{src.name}” would take more than {MAX_TOTAL // 1024 ** 3} GB once extracted, "
                            f"which looks like a zip bomb, so DragShift didn't extract it.")
        names.append((name, e))
    p = Plan(total)
    tops = {n.split("/", 1)[0] for n, _ in names}
    if strip and len(tops) == 1:
        top = next(iter(tops))
        nested = all(n != top or e.kind == "dir" for n, e in names)
        if nested and any("/" in n for n, _ in names):
            p.prefix = top
    for name, e in names:
        if e.kind == "link" and not _link_ok(_strip(name, p.prefix), e.link):
            raise _unsafe(src, f"{name} → {e.link}")
    return p


def _inside(root: Path, path: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- extract
def _write_entries(entries: Iterator[Entry], p: Plan, root: Path, src: Path):
    """Write the planned entries under `root`. Names are cleaned again (a stream is decoded a
    second time) and the byte cap is enforced on what's really written, not the stated sizes."""
    progress = _Progress(p.total)
    links: list[tuple[Path, str, str]] = []
    for e in entries:
        name = clean_name(e.raw, src)
        if name is None or e.kind == "other":
            continue
        if p.prefix and name == p.prefix:
            continue                       # the dropped top folder itself
        target = root / _strip(name, p.prefix)
        if not _inside(root, target.parent if e.kind != "dir" else target):
            raise _unsafe(src, name)
        if e.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        elif e.kind == "link":
            links.append((target, e.link, name))
        elif e.kind == "hardlink":
            # copied, not linked: the copy can never share an inode with a file outside
            link_rel = _strip(clean_name(e.link, src) or "", p.prefix)
            source = root / link_rel
            if not link_rel or source.is_symlink() or not source.is_file() or not _inside(root, source):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise _unsafe(src, name)
            with open(source, "rb") as f_in, open(target, "wb") as f_out:
                _copy(f_in, f_out, progress, src)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise _unsafe(src, name)
            with e.open() as f_in, open(target, "wb") as f_out:
                _copy(f_in, f_out, progress, src)
            _set_meta(target, e)
    for target, link, name in links:
        _make_link(root, target, link, name, src)


def _copy(f_in: BinaryIO, f_out: BinaryIO, progress: _Progress, src: Path):
    """Copy in chunks, counting what's really written against the cap: a zip's stated sizes
    can lie, and a single .gz states none."""
    while True:
        chunk = f_in.read(CHUNK)
        if not chunk:
            return
        if progress.done + len(chunk) > MAX_TOTAL:
            raise UserError(f"“{src.name}” would take more than {MAX_TOTAL // 1024 ** 3} GB once extracted, "
                            f"which looks like a zip bomb, so DragShift stopped.")
        f_out.write(chunk)
        progress.add(len(chunk))


def _set_meta(target: Path, e: Entry):
    """Keep the executable bit and the date; never setuid/setgid, always readable by the owner."""
    try:
        if e.mode & 0o111:
            os.chmod(target, (e.mode & 0o777) | 0o600)
        if e.mtime:
            os.utime(target, (e.mtime, e.mtime))
    except (OSError, OverflowError, ValueError):
        pass


def _make_link(root: Path, target: Path, link: str, name: str, src: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _inside(root, target.parent) or target.exists() or target.is_symlink():
        raise _unsafe(src, name)
    try:
        os.symlink(link.replace("\\", "/") if os.sep == "/" else link, target)
    except (OSError, NotImplementedError):
        return       # Windows without the symlink privilege: skip the link, keep the rest
    if not _inside(root, target):     # catches chains such as a → "." then b → "a/.."
        target.unlink()
        raise _unsafe(src, f"{name} → {link}")


def _check_space(folder: Path, need: int):
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if need > free:
        raise UserError(f"Not enough free space: this needs about {need / 1024 ** 3:.1f} GB.")


def extract_into(src: str | Path, root: Path, strip: bool = True) -> None:
    """Extract every safe entry of `src` into the existing, empty folder `root`."""
    src = Path(src)
    with _reader(src) as entries:
        p = plan(entries(), src, strip)
        _check_space(root, p.total)
        _write_entries(entries(), p, root, src)


def _decompress(src: Path) -> Path:
    """A single .gz/.bz2/.xz file becomes the file it holds, beside it."""
    out = unique_path(output_dir(src) / base_name(src))     # "notes (1).txt", like other results

    def write(dst):
        # the compressed size is all we know up front: progress assumes about 4:1
        progress = _Progress(src.stat().st_size * 4)
        with _reader(src) as entries:
            entry = next(entries())
            with entry.open() as f_in, open(dst, "wb") as f_out:
                _copy(f_in, f_out, progress, src)

    atomic_save(write, out)
    return out


def extract(src: str | Path) -> Path:
    """Extract into a new folder beside the archive, named after it ('Photos/' for
    'Photos.zip'). An archive whose entries all sit in one folder doesn't get a second level."""
    src = Path(src)
    if kind(src) == "single":
        return _decompress(src)
    folder = output_dir(src)
    tmp = temp_path(folder / base_name(src))
    tmp.mkdir()
    try:
        extract_into(src, tmp)
        dest = unique_name(folder, base_name(src))
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def convert(src: str | Path, target: str) -> Path:
    """Re-pack an archive in another format ('Photos.tar.gz' -> 'Photos.zip'), keeping its
    folder structure exactly. Extracts (with every check above) to a temp folder first."""
    src, target = Path(src), target.upper()
    if target not in CREATE_FORMATS:
        raise ValueError(f"Can't convert an archive to {target}")
    out = unique_name(output_dir(src), base_name(src), CREATE_FORMATS[target])
    with tempfile.TemporaryDirectory(prefix="dragshift-") as tmp:
        root = Path(tmp)
        extract_into(src, root, strip=False)
        items = collect(sorted(root.iterdir(), key=lambda p: p.name)) if any(root.iterdir()) else []
        if not items:
            raise UserError(f"“{src.name}” is empty.")
        atomic_save(lambda dst: write_archive(Path(dst), items, target), out)
    return out


def count_files(paths: list[str | Path]) -> int:
    """Files inside the given files and folders (links count as files)."""
    n = 0
    for p in map(Path, paths):
        if p.is_dir() and not p.is_symlink():
            n += sum(len(fs) + sum(os.path.islink(os.path.join(r, d)) for d in ds) for r, ds, fs in os.walk(p))
        else:
            n += 1
    return n
