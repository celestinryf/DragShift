"""Put the pinned ffmpeg + ffprobe for this platform into vendor/ffmpeg and add the licence texts:
downloaded and SHA-256 checked on Windows and Linux, built from source on macOS. CI runs this
before the tests and PyInstaller (DragShift.spec bundles whatever is in vendor/ffmpeg). The app
itself never downloads anything.

    python packaging/ffmpeg/fetch.py [platform] [--github-path]

Builds (all FFmpeg 9.0.2, all LGPL v3):
- Windows x64, Linux amd64: BtbN/FFmpeg-Builds LGPL v3 (no libx264; H.264 via h264_mf / OpenH264).
  The month-end autobuild tag is kept by BtbN for about two years; mirror it before then.
- macOS arm64, x86_64: built by build_mac.py from the official source tarball plus pinned LAME,
  Opus, Ogg/Vorbis and libvpx sources (no libx264; H.264 via VideoToolbox). There is no
  trustworthy prebuilt LGPL macOS build, so we make our own. It can only target the Mac it runs
  on, so CI builds on an Apple silicon and an Intel runner.
ffmpeg is shipped as separate programs that DragShift runs as subprocesses, with the licence texts
and the source location bundled beside them.
Every hash below was checked by downloading the file and running `shasum -a 256` on it."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEST = ROOT / "vendor" / "ffmpeg"
VERSION = "9.0.2"
SOURCE = "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz"

_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-30-13-08/"
_BTBN_DIR = "ffmpeg-n9.0.2-17-g2a571b6068-{}-lgpl-9.0"

# platform -> licence and archives; each archive maps a member path to the file name it gets
BUILDS = {
    "windows-x64": {
        "licence": "LGPL-3.0-or-later",
        "source": SOURCE + " (build scripts: https://github.com/BtbN/FFmpeg-Builds)",
        "archives": [{
            "url": _BTBN + _BTBN_DIR.format("win64") + ".zip",
            "sha256": "6b264b9e6019103f601d98c292bd332fd87acf1c5e941ddff4fb71760fe63432",
            "members": {_BTBN_DIR.format("win64") + "/bin/ffmpeg.exe": "ffmpeg.exe",
                        _BTBN_DIR.format("win64") + "/bin/ffprobe.exe": "ffprobe.exe",
                        _BTBN_DIR.format("win64") + "/LICENSE.txt": "LICENSE-ffmpeg-build.txt"},
        }],
    },
    "linux-x64": {
        "licence": "LGPL-3.0-or-later",
        "source": SOURCE + " (build scripts: https://github.com/BtbN/FFmpeg-Builds)",
        "archives": [{
            "url": _BTBN + _BTBN_DIR.format("linux64") + ".tar.xz",
            "sha256": "2d41cbea0ca1a15029b638330740f78d6f5aeb062355bf425433fc938705350a",
            "members": {_BTBN_DIR.format("linux64") + "/bin/ffmpeg": "ffmpeg",
                        _BTBN_DIR.format("linux64") + "/bin/ffprobe": "ffprobe",
                        _BTBN_DIR.format("linux64") + "/LICENSE.txt": "LICENSE-ffmpeg-build.txt"},
        }],
    },
    # built locally by build_mac.py, which pins and hash-checks every source tarball
    "macos-arm64": {"licence": "LGPL-3.0-or-later", "builder": "build_mac.py"},
    "macos-x64": {"licence": "LGPL-3.0-or-later", "builder": "build_mac.py"},
}
# licence texts kept in the repo (copied from FFmpeg's n9.0.2 tag). The LGPL v3 is written as extra
# permissions on top of the GPL v3, so both texts go with every LGPL v3 build.
LICENCE_FILES = {"LGPL-3.0-or-later": ["LICENSE.md", "COPYING.LGPLv3", "COPYING.GPLv3"]}


def current_platform() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform == "win32":
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(data: bytes, expected: str, url: str) -> None:
    got = sha256(data)
    if got != expected:
        raise SystemExit(f"SHA-256 mismatch for {url}\n  expected {expected}\n  got      {got}")


def download(url: str) -> bytes:
    print(f"Downloading {url}", flush=True)
    # some hosts refuse Python's default User-Agent
    req = urllib.request.Request(url, headers={"User-Agent": "DragShift-build/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def extract(data: bytes, url: str, members: dict[str, str], dest: Path) -> None:
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member, name in members.items():
                (dest / name).write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            for member, name in members.items():
                f = t.extractfile(member)
                if f is None:
                    raise SystemExit(f"{member} is missing from {url}")
                (dest / name).write_bytes(f.read())
    for name in members.values():
        if not name.endswith((".txt", ".md")):
            p = dest / name
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_notice(licence: str, key: str, origin: str, source: str, dest: Path) -> None:
    (dest / "FFMPEG-NOTICE.txt").write_text(
        f"DragShift bundles FFmpeg {VERSION} ({key}) as separate programs (ffmpeg, ffprobe)\n"
        f"that it runs as subprocesses. FFmpeg is not part of DragShift's MIT licence.\n\n"
        f"Licence of this build: {licence} (see the COPYING and LICENSE files here).\n"
        f"{origin}\n"
        f"Corresponding source: {source}\n"
        f"FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.\n",
        encoding="utf-8")


def _build_mac():
    """build_mac.py sits next to this file; it is only needed (and only works) on macOS."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import build_mac
    return build_mac


def fetch(key: str, dest: Path = DEST) -> Path:
    if key not in BUILDS:
        raise SystemExit(f"No pinned ffmpeg for {key}. Known: {', '.join(BUILDS)}")
    build = BUILDS[key]
    if "builder" in build and key != current_platform():
        raise SystemExit(f"{key} is built from source and can only be built on {key} itself.")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if "builder" in build:
        origin, source = _build_mac().install(dest)
    else:
        for archive in build["archives"]:
            url = archive["url"]
            data = download(url)
            verify(data, archive["sha256"], url)
            extract(data, url, archive["members"], dest)
        origin = f"Downloaded from: {', '.join(a['url'] for a in build['archives'])}"
        source = build["source"]
    for name in LICENCE_FILES[build["licence"]]:
        shutil.copy2(HERE / name, dest / name)
    write_notice(build["licence"], key, origin, source, dest)
    print(f"ffmpeg {VERSION} ({build['licence']}) ready in {dest}", flush=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("platform", nargs="?", default=current_platform(), choices=sorted(BUILDS))
    ap.add_argument("--github-path", action="store_true",
                    help="add vendor/ffmpeg to PATH for later GitHub Actions steps (tests use it)")
    args = ap.parse_args(argv)
    dest = fetch(args.platform)
    if args.github_path and os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as f:
            f.write(str(dest) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
