"""Build an LGPL v3 ffmpeg + ffprobe for macOS from the official source tarballs, for the CPU of
the Mac it runs on. fetch.py calls this for the macos-* platforms; it can also be run by hand:

    python packaging/ffmpeg/build_mac.py [--work DIR] [--dest DIR]

No --enable-gpl and no --enable-nonfree: H.264 comes from Apple's VideoToolbox instead of x264.
LAME (MP3), Opus, Ogg/Vorbis and libvpx (VP9 for WebM) are built as static libraries from pinned,
SHA-256 checked tarballs, so the two programs only link to macOS's own libraries (checked with
otool -L after every build). Needs Xcode's command line tools and pkg-config, plus nasm on Intel
Macs (`brew install pkgconf nasm`). Nothing from Homebrew ends up in the binaries.

There is one build per architecture, not a universal binary: CI already builds the Apple silicon
and Intel apps on matching runners, and each .dmg only needs its own half.

A finished build is kept in <work>/out next to a stamp of this file's hash, so running it again
(or a CI cache hit) skips the build until this file changes."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch  # noqa: E402  (shared paths, download and hash check)

MIN_MACOS = "11.0"   # matches LSMinimumSystemVersion in DragShift.spec
JOBS = str(os.cpu_count() or 4)

# Each hash was computed with `shasum -a 256` on the file downloaded from the official URL below,
# and cross-checked: ffmpeg, lame and libvpx against Homebrew's formulas for the same versions,
# opus, libogg and libvorbis against the SHA256SUMS files Xiph publishes next to them.
SOURCES = {
    "ffmpeg": {"url": fetch.SOURCE, "dir": f"ffmpeg-{fetch.VERSION}",
               "sha256": "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e",
               "licences": []},   # fetch.LICENCE_FILES covers ffmpeg itself
    "lame": {"url": "https://downloads.sourceforge.net/project/lame/lame/4.0/lame-4.0.tar.gz",
             "dir": "lame-4.0",
             "sha256": "3df5124d5ad3a98312ffd7ba6a9b36230e4f8a3e66d3ce0f425e336c32d216eb",
             "licences": ["COPYING"]},
    "libogg": {"url": "https://downloads.xiph.org/releases/ogg/libogg-1.3.6.tar.xz", "dir": "libogg-1.3.6",
               "sha256": "5c8253428e181840cd20d41f3ca16557a9cc04bad4a3d04cce84808677fa1061",
               "licences": ["COPYING"]},
    "libvorbis": {"url": "https://downloads.xiph.org/releases/vorbis/libvorbis-1.3.7.tar.xz",
                  "dir": "libvorbis-1.3.7",
                  "sha256": "b33cc4934322bcbf6efcbacf49e3ca01aadbea4114ec9589d1b1e9d20f72954b",
                  "licences": ["COPYING"],
                  # its configure passes a flag Apple's current linker rejects (Homebrew drops it too)
                  "configure_drop": " -force_cpusubtype_ALL"},
    "opus": {"url": "https://downloads.xiph.org/releases/opus/opus-1.6.1.tar.gz", "dir": "opus-1.6.1",
             "sha256": "6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1",
             "licences": ["COPYING"]},
    "libvpx": {"url": "https://github.com/webmproject/libvpx/archive/refs/tags/v1.17.0.tar.gz",
               "dir": "libvpx-1.17.0",
               "sha256": "1020f184046187baa2985dbde38e0691f49c44088bca7a1842b0236c6081dc0a",
               "licences": ["LICENSE", "PATENTS"]},
}

# Everything ffmpeg can do without an external library stays in (decoders, muxers, filters).
# --disable-autodetect stops configure from picking up whatever the build machine has installed
# (SDL, xz, Homebrew libraries...); the system parts we want are switched back on by name.
FFMPEG_FLAGS = [
    "--enable-version3",          # LGPL v3, the same licence as the BtbN builds on Windows/Linux
    "--disable-autodetect",
    "--enable-videotoolbox",      # h264_videotoolbox / hevc_videotoolbox, the LGPL route to H.264
    "--enable-audiotoolbox",
    "--enable-zlib",              # PNG frames and compressed Matroska/MP4 headers (/usr/lib/libz)
    "--enable-bzlib",             # bzip2-compressed Matroska tracks (/usr/lib/libbz2)
    "--enable-libmp3lame",
    "--enable-libopus",
    "--enable-libvorbis",
    "--enable-libvpx",
    "--disable-network",          # DragShift only reads local files (and passes them as file:)
    "--disable-ffplay",
    "--disable-doc",
    "--disable-debug",
    "--disable-shared",
    "--enable-static",
    "--pkg-config-flags=--static",
]
# Encoders media.py asks for; the build fails rather than ship an ffmpeg that is missing one
REQUIRED_ENCODERS = {"h264_videotoolbox", "mpeg4", "libvpx-vp9", "aac", "libmp3lame", "libopus",
                     "libvorbis", "ac3", "flac", "pcm_s16le", "pcm_s24be", "png", "gif", "ffv1"}
FORBIDDEN_FLAGS = ("--enable-gpl", "--enable-nonfree")
SYSTEM_LIBS = ("/usr/lib/", "/System/Library/")
# build settings a developer's shell might export that would point the compiler at other libraries
LEAKY_ENV = ("CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "CPATH", "C_INCLUDE_PATH", "LIBRARY_PATH",
             "PKG_CONFIG_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")
PROGRAMS = ("ffmpeg", "ffprobe")


def arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def stamp() -> str:
    """Changes whenever this file (pins, flags, steps) or the target CPU changes."""
    return hashlib.sha256(Path(__file__).read_bytes() + arch().encode()).hexdigest()


def default_work() -> Path:
    return fetch.ROOT / "vendor" / "ffmpeg-build" / fetch.current_platform()


def configure_line() -> str:
    return " ".join(["./configure", *FFMPEG_FLAGS])


def build_env(prefix: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in LEAKY_ENV}
    flags = f"-O2 -mmacosx-version-min={MIN_MACOS}"
    env.update(MACOSX_DEPLOYMENT_TARGET=MIN_MACOS, CFLAGS=flags, LDFLAGS=flags,
               CPPFLAGS=f"-I{prefix / 'include'}",
               # only our own .pc files: Homebrew's pkg-config would otherwise offer its libraries
               PKG_CONFIG_LIBDIR=str(prefix / "lib" / "pkgconfig"))
    return env


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def unpack(name: str, work: Path) -> Path:
    """Download (once, into <work>/downloads) and hash-check a source tarball, then unpack it fresh."""
    src = SOURCES[name]
    tarball = work / "downloads" / src["url"].rsplit("/", 1)[-1]
    if not tarball.is_file() or fetch.sha256(tarball.read_bytes()) != src["sha256"]:
        data = fetch.download(src["url"])
        fetch.verify(data, src["sha256"], src["url"])
        tarball.parent.mkdir(parents=True, exist_ok=True)
        tarball.write_bytes(data)
    tree = work / "src" / src["dir"]
    if tree.exists():
        shutil.rmtree(tree)
    tree.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as t:
        t.extractall(tree.parent, filter="data")
    if not tree.is_dir():
        raise SystemExit(f"{tarball.name} did not unpack to {src['dir']}")
    if "configure_drop" in src:
        configure = tree / "configure"
        text = configure.read_text()
        if src["configure_drop"] not in text:
            raise SystemExit(f"{name}: expected {src['configure_drop']!r} in configure; check the pin")
        configure.write_text(text.replace(src["configure_drop"], ""))
    return tree


def autotools(name: str, work: Path, prefix: Path, env: dict[str, str], *flags: str) -> None:
    tree = unpack(name, work)
    run(["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static", *flags], tree, env)
    run(["make", f"-j{JOBS}"], tree, env)
    run(["make", "install"], tree, env)


def build_libraries(work: Path, prefix: Path, env: dict[str, str]) -> None:
    autotools("lame", work, prefix, env, "--disable-frontend", "--disable-decoder", "--disable-gtktest")
    autotools("libogg", work, prefix, env)
    autotools("libvorbis", work, prefix, env, f"--with-ogg={prefix}", "--disable-oggtest",
              "--disable-docs", "--disable-examples")
    autotools("opus", work, prefix, env, "--disable-doc", "--disable-extra-programs")
    vpx = unpack("libvpx", work)
    # darwin20 is macOS 11; naming it keeps configure from guessing on newer macOS releases
    run(["./configure", f"--prefix={prefix}", f"--target={arch()}-darwin20-gcc", "--disable-shared",
         "--enable-static", "--enable-pic", "--disable-examples", "--disable-tools", "--disable-docs",
         "--disable-unit-tests", f"--extra-cflags=-mmacosx-version-min={MIN_MACOS}"], vpx, env)
    run(["make", f"-j{JOBS}"], vpx, env)
    run(["make", "install"], vpx, env)


def build_ffmpeg(work: Path, prefix: Path, env: dict[str, str]) -> Path:
    tree = unpack("ffmpeg", work)
    run(["./configure", f"--prefix={prefix}", "--cc=clang", *FFMPEG_FLAGS,
         f"--extra-cflags=-mmacosx-version-min={MIN_MACOS} -I{prefix / 'include'}",
         f"--extra-ldflags=-mmacosx-version-min={MIN_MACOS} -L{prefix / 'lib'}"], tree, env)
    run(["make", f"-j{JOBS}"], tree, env)
    return tree


def foreign_libs(otool_output: str) -> list[str]:
    """Libraries in `otool -L` output that are not part of macOS (the first line is the file)."""
    libs = [line.split()[0] for line in otool_output.splitlines()[1:] if line.strip()]
    return [lib for lib in libs if not lib.startswith(SYSTEM_LIBS)]


def encoder_names(listing: str) -> set[str]:
    """Names from `ffmpeg -encoders` (rows look like " V....D libvpx-vp9  libvpx VP9")."""
    names = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS" and parts[1] != "=":
            names.add(parts[1])
    return names


def _output(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def check(out: Path) -> None:
    """Refuse a build that links to non-system libraries, is not LGPL, or lacks an encoder."""
    for name in PROGRAMS:
        bad = foreign_libs(_output(["otool", "-L", str(out / name)]))
        if bad:
            raise SystemExit(f"{name} links to libraries outside macOS: {', '.join(bad)}")
    ffmpeg = str(out / "ffmpeg")
    version = _output([ffmpeg, "-hide_banner", "-version"])
    if any(flag in version for flag in FORBIDDEN_FLAGS):
        raise SystemExit(f"ffmpeg was configured with GPL or nonfree parts:\n{version}")
    licence = _output([ffmpeg, "-hide_banner", "-L"])
    if "GNU Lesser General Public License" not in licence or "version 3" not in licence:
        raise SystemExit(f"ffmpeg does not report LGPL v3:\n{licence}")
    missing = REQUIRED_ENCODERS - encoder_names(_output([ffmpeg, "-hide_banner", "-encoders"]))
    if missing:
        raise SystemExit(f"ffmpeg is missing encoders DragShift uses: {', '.join(sorted(missing))}")


def build(work: Path | None = None) -> Path:
    """Build (or reuse) ffmpeg + ffprobe and return the folder holding them and the library licences."""
    if sys.platform != "darwin":
        raise SystemExit("build_mac.py only runs on macOS.")
    work = work or default_work()
    out = work / "out"
    if (out / "STAMP").is_file() and (out / "STAMP").read_text().strip() == stamp():
        print(f"Reusing the ffmpeg build in {out}", flush=True)
        check(out)
        return out
    prefix = work / "prefix"
    for d in (prefix, out):
        if d.exists():
            shutil.rmtree(d)
    env = build_env(prefix)
    build_libraries(work, prefix, env)
    tree = build_ffmpeg(work, prefix, env)
    out.mkdir(parents=True)
    for name in PROGRAMS:
        shutil.copy2(tree / name, out / name)
    for lib, src in SOURCES.items():
        for licence in src["licences"]:
            shutil.copy2(work / "src" / src["dir"] / licence, out / f"LICENSE-{lib}-{licence}.txt")
    check(out)
    (out / "STAMP").write_text(stamp() + "\n")
    print(f"ffmpeg {fetch.VERSION} (LGPL v3, {arch()}) built in {out}", flush=True)
    return out


def install(dest: Path, work: Path | None = None) -> tuple[str, str]:
    """Copy a build into `dest`; returns the notice's "how it was made" and source lines."""
    out = build(work)
    for f in sorted(out.iterdir()):
        if f.name != "STAMP":
            shutil.copy2(f, dest / f.name)
    # CI knows which repository and commit it built from; a local build just names the script
    repo = os.environ.get("GITHUB_REPOSITORY")
    where = f"https://github.com/{repo} at commit {os.environ.get('GITHUB_SHA', '?')}" if repo else "DragShift"
    origin = (f"Built from source for {arch()} macOS {MIN_MACOS}+ by packaging/ffmpeg/build_mac.py in "
              f"{where}, configured with:\n  {configure_line()}")
    source = ", ".join(src["url"] for src in SOURCES.values())
    return origin, source


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, default=None, help=f"build folder (default {default_work()})")
    ap.add_argument("--dest", type=Path, default=None, help="also copy the programs and licences here")
    args = ap.parse_args(argv)
    out = build(args.work)
    if args.dest:
        args.dest.mkdir(parents=True, exist_ok=True)
        install(args.dest, args.work)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
