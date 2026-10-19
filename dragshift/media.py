"""Video and audio through ffmpeg. This is the only module that runs ffmpeg or ffprobe.

Every operation takes a source path and returns the new file written next to it
(`util.sibling`), saved through `convert.atomic_save` so a half-written file never shows up.
File names are untrusted: ffmpeg gets argument lists (never a shell) and every path goes in
as `file:/absolute/path`, so names like `-rf.mp4` or `http:x.mp4` are read as plain files."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .convert import atomic_save
from .util import UserError, file_size, sibling

VIDEO_TARGETS = ["MP4", "MOV", "MKV", "WEBM", "AVI", "GIF", "MP3", "M4A", "WAV"]
AUDIO_TARGETS = ["MP3", "M4A", "WAV", "FLAC", "OGG", "OPUS", "AIFF"]

MISSING = "Video and audio need ffmpeg, and DragShift couldn't find it. Install ffmpeg and try again."
UNREADABLE = "This file looks damaged, or it isn't a video or audio format DragShift can read."

GIF_WIDTH = 640          # wider GIFs get huge fast and gain little
GIF_FPS = 15
GIF_MAX_SECONDS = 60     # a minute of 640 px GIF is already tens of MB
CRF_CONVERT = 20         # x264 quality for conversions and edits (lower = better; 23 is x264's default)
CRF_COMPRESS = 28        # x264 quality for Compress: about half the size of CRF 23, still clean
PERCENT_MUX_SHARE = 0.97  # of a size-planned bitrate left for streams; the rest is container overhead
MIN_VIDEO_BITRATE = 50_000   # bits/s; below this H.264 is mush
VP9_CRF_CONVERT = 32
VP9_CRF_COMPRESS = 40
SPEED_RANGE = (0.25, 4.0)
MIN_TRIM = 0.05          # seconds; anything shorter is almost certainly a slip of the mouse
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"   # EBU R128 targets used by most streaming services
LOSSY_AUDIO = {"mp3", "aac", "vorbis", "opus", "wmav1", "wmav2", "ac3", "eac3"}


# ---------------------------------------------------------------- finding ffmpeg
# GUI apps on macOS don't inherit the shell's PATH, so Homebrew/MacPorts locations are checked too
EXTRA_DIRS = {"darwin": ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]}


def _bundled_dirs() -> list[Path]:
    """Where the packaged app keeps its own ffmpeg (DragShift.spec puts it in an `ffmpeg` folder)."""
    dirs = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(Path(base) / "ffmpeg")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        dirs += [exe_dir / "ffmpeg", exe_dir.parent / "Frameworks" / "ffmpeg",
                 exe_dir.parent / "Resources" / "ffmpeg"]
    return dirs


@lru_cache(maxsize=None)
def find_tool(name: str) -> str | None:
    """Full path of `ffmpeg` / `ffprobe`: bundled copy first, then PATH, then common install dirs."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in _bundled_dirs():
        p = d / exe
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_DIRS.get(sys.platform, []):
        p = Path(d) / exe
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def available() -> bool:
    return find_tool("ffmpeg") is not None and find_tool("ffprobe") is not None


def _tool(name: str) -> str:
    path = find_tool(name)
    if not path:
        raise UserError(MISSING)
    return path


def _log(msg: str) -> None:
    """Details for the log file (the toast only gets the plain sentence)."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001  (pythonw has no stderr before logging is set up)
        pass


def _file(path: str | Path) -> str:
    """ffmpeg reads `-x.mp4` as an option and `name:...` as a protocol; `file:/abs` is always a file."""
    return "file:" + os.path.abspath(path)


def _quiet() -> dict:
    """No stdin, and no console window flashing up on Windows."""
    kw: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


@lru_cache(maxsize=None)
def _encoders(ffmpeg: str) -> frozenset[str]:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                           timeout=30, encoding="utf-8", errors="replace", **_quiet())
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"ffmpeg -encoders failed: {e}")
        return frozenset()
    names = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        # rows look like " V....D libx264   libx264 H.264 ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS" and parts[1] != "=":
            names.add(parts[1])
    return frozenset(names)


def has_encoder(name: str) -> bool:
    return name in _encoders(_tool("ffmpeg"))


@lru_cache(maxsize=None)
def _encoder_works(ffmpeg: str, encoder: str) -> bool:
    """Hardware encoders can be listed but unusable (no GPU, a VM, an old OS): try a tiny encode once."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
           "color=c=gray:size=128x128:rate=10:duration=0.5", "-c:v", encoder, "-b:v", "500k", "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30, **_quiet()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _usable_video_encoder(encoder: str) -> bool:
    return has_encoder(encoder) and _encoder_works(_tool("ffmpeg"), encoder)


def _need(encoder: str, what: str) -> str:
    if not has_encoder(encoder):
        raise UserError(f"This copy of ffmpeg can't make {what} (it has no {encoder} encoder).")
    return encoder


# ---------------------------------------------------------------- probing
@dataclass
class Stream:
    index: int
    kind: str                 # "video" | "audio"
    codec: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    rotation: int = 0
    sample_rate: int = 0
    channels: int = 0
    bits: int = 0
    bit_rate: int = 0


@dataclass
class Info:
    duration: float
    format: str
    bit_rate: int = 0
    video: list[Stream] = field(default_factory=list)   # real video only, not cover art
    audio: list[Stream] = field(default_factory=list)

    @property
    def size(self) -> tuple[int, int] | None:
        """Display size (phone clips stored sideways report their upright size)."""
        if not self.video:
            return None
        v = self.video[0]
        return (v.height, v.width) if v.rotation % 180 else (v.width, v.height)


def _ratio(text: str | None) -> float:
    try:
        num, _, den = (text or "").partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _stream(s: dict) -> Stream | None:
    kind = s.get("codec_type")
    if kind == "video":
        if (s.get("disposition") or {}).get("attached_pic"):
            return None   # album art inside an MP3/M4A
        rot = 0
        for sd in s.get("side_data_list") or []:
            if "rotation" in sd:
                rot = _int(sd["rotation"])
        rot = rot or _int((s.get("tags") or {}).get("rotate"))
        return Stream(_int(s.get("index")), "video", s.get("codec_name", ""), _int(s.get("width")),
                      _int(s.get("height")), _ratio(s.get("avg_frame_rate")) or _ratio(s.get("r_frame_rate")),
                      rot, bit_rate=_int(s.get("bit_rate")))
    if kind == "audio":
        # only lossless sources report a real bit depth; decoded MP3/AAC say 0 and become 16-bit
        bits = _int(s.get("bits_per_raw_sample")) or _int(s.get("bits_per_sample"))
        return Stream(_int(s.get("index")), "audio", s.get("codec_name", ""), sample_rate=_int(s.get("sample_rate")),
                      channels=_int(s.get("channels")), bits=bits, bit_rate=_int(s.get("bit_rate")))
    return None


def probe(path: str | Path) -> Info:
    """Duration, container and streams of a media file (raises UserError if it can't be read)."""
    cmd = [_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
           _file(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8",
                           errors="replace", **_quiet())
    except OSError as e:
        _log(f"ffprobe could not start: {e}")
        raise UserError(MISSING) from e
    except subprocess.TimeoutExpired as e:
        raise UserError(UNREADABLE) from e
    if r.returncode != 0:
        if not Path(path).exists():
            raise FileNotFoundError(path)
        _log(f"ffprobe failed on {path}: {_last_line(r.stderr)}")
        raise UserError(UNREADABLE)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise UserError(UNREADABLE) from e
    fmt = data.get("format") or {}
    streams = [st for st in (_stream(s) for s in data.get("streams") or []) if st is not None]
    duration = _ratio(fmt.get("duration")) or max(
        (_ratio(s.get("duration")) for s in data.get("streams") or []), default=0.0)
    info = Info(duration, fmt.get("format_name", ""), _int(fmt.get("bit_rate")),
                [s for s in streams if s.kind == "video"], [s for s in streams if s.kind == "audio"])
    if not info.video and not info.audio:
        raise UserError(UNREADABLE)
    return info


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def fmt_duration(seconds: float) -> str:
    """0:42, 3:07, 1:02:03."""
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- running ffmpeg
class Cancelled(UserError):
    """The user stopped the job."""


_ctx = threading.local()


@contextmanager
def reporting(progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None):
    """Lets a background job hear progress from (and cancel) every ffmpeg run made inside it,
    without threading callbacks through each conversion function."""
    prev = getattr(_ctx, "hooks", (None, None))
    _ctx.hooks = (progress, cancel)
    try:
        yield
    finally:
        _ctx.hooks = prev


def hooks() -> tuple[Callable[[float], None] | None, threading.Event | None]:
    """The (progress, cancel) pair of the job running on this thread, for long work that isn't
    ffmpeg (archives)."""
    return getattr(_ctx, "hooks", (None, None))


# Last-line ffmpeg messages that have a plainer explanation
FRIENDLY = [
    ("no space left", "The disk is full."),
    ("permission denied", "No permission to write next to this file. Try a folder you own."),
    ("invalid data found", UNREADABLE),
    ("moov atom not found", UNREADABLE),
    ("does not contain any stream", UNREADABLE),
]


def _explain(tail: list[str]) -> str:
    text = " ".join(tail).lower()
    for needle, msg in FRIENDLY:
        if needle in text:
            return msg
    return "ffmpeg couldn't process this file."


def _wait(proc: subprocess.Popen, step: Callable, cancel: threading.Event | None,
          timeout: float | None = None):
    """Wait for ffmpeg in short steps, so a cancel (or `timeout` seconds passing) stops it
    promptly. `step` is proc.wait, or proc.communicate when nothing else reads the pipes; its
    result is returned. A stopped ffmpeg is reaped before raising, so none is left running."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            return step(timeout=0.1)
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or (deadline is not None and time.monotonic() > deadline):
                proc.kill()
                step()
                if cancelled:
                    raise Cancelled("Cancelled.") from None
                raise


def _execute(cmd: list[str], duration: float, progress, cancel) -> None:
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", **_quiet())
    except OSError as e:
        _log(f"ffmpeg could not start: {e}")
        raise UserError(MISSING) from e
    tail: deque[str] = deque(maxlen=12)

    def read_err():
        for line in proc.stderr:
            if line.strip():
                tail.append(line.strip())

    def read_progress():
        last = -1.0
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if progress is None:
                continue
            frac = None
            if key in ("out_time_us", "out_time_ms") and duration > 0:   # both are microseconds
                try:
                    frac = min(1.0, max(0.0, int(val) / 1e6 / duration))
                except ValueError:
                    continue   # "N/A" before the first frame
            elif key == "progress" and val == "end":
                frac = 1.0
            if frac is not None and (frac - last >= 0.01 or (frac == 1.0 and last < 1.0)):
                last = frac
                try:
                    progress(frac)
                except Exception as e:  # noqa: BLE001  (a UI hiccup must not kill the encode)
                    _log(f"progress callback failed: {e}")

    readers = [threading.Thread(target=read_err, daemon=True),
               threading.Thread(target=read_progress, daemon=True)]
    for t in readers:
        t.start()
    try:
        _wait(proc, proc.wait, cancel)
    finally:
        for t in readers:
            t.join(timeout=2)
    if proc.returncode != 0:
        _log(f"ffmpeg failed (exit {proc.returncode}): {tail[-1] if tail else 'no message'}\n"
             f"  command: {cmd}")
        raise UserError(_explain(list(tail)))


def run(args: list[str], out: Path, muxer: str, duration: float = 0.0,
        progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None) -> Path:
    """Run ffmpeg with `args` (inputs and codec options) and write `out` atomically.

    The muxer is always given explicitly because the temp file's name has no useful extension."""
    ffmpeg = _tool("ffmpeg")
    hooks = getattr(_ctx, "hooks", (None, None))
    progress = progress or hooks[0]
    cancel = cancel or hooks[1]

    def write(tmp: Path):
        cmd = [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", *args, "-f", muxer, _file(tmp)]
        _execute(cmd, duration, progress, cancel)
        if not tmp.exists() or tmp.stat().st_size == 0:
            _log(f"ffmpeg wrote nothing: {cmd}")
            raise UserError("ffmpeg finished but didn't write anything.")

    atomic_save(write, out)
    return out


def make_test_clip(out: str | Path, seconds: float = 1.0, size: str = "160x120", audio: bool = True,
                   video: bool = True, vcodec: str = "mpeg4", acodec: str = "aac", muxer: str = "mp4",
                   extra: list[str] | None = None) -> Path:
    """A short generated clip (test pattern and/or a 440 Hz tone) for tests and the frozen app's
    self-test. The default codecs are built into every ffmpeg, LGPL builds included."""
    args = []
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    args += ["-c:v", vcodec, "-pix_fmt", "yuv420p"] if video else ["-vn"]
    args += ["-c:a", acodec] if audio else ["-an"]
    return run(args + (extra or []), Path(out), muxer, seconds)


# ---------------------------------------------------------------- containers and codecs
@dataclass(frozen=True)
class Container:
    muxer: str
    ext: str
    video: tuple[str, ...] = ()    # codecs that can be stream-copied in as-is
    audio: tuple[str, ...] = ()


CONTAINERS = {
    "MP4": Container("mp4", ".mp4", ("h264", "hevc", "av1", "mpeg4"), ("aac", "mp3", "alac", "ac3", "eac3")),
    "MOV": Container("mov", ".mov", ("h264", "hevc", "prores", "mjpeg", "mpeg4"),
                     ("aac", "alac", "mp3", "pcm_s16le", "pcm_s24le", "ac3")),
    "MKV": Container("matroska", ".mkv",
                     ("h264", "hevc", "av1", "vp8", "vp9", "mpeg4", "mpeg2video", "prores", "mjpeg", "theora",
                      "ffv1"),
                     ("aac", "mp3", "opus", "vorbis", "flac", "alac", "ac3", "eac3", "dts", "truehd",
                      "pcm_s16le", "pcm_s24le")),
    "WEBM": Container("webm", ".webm", ("vp8", "vp9", "av1"), ("opus", "vorbis")),
    "AVI": Container("avi", ".avi", ("mpeg4", "h264", "mjpeg", "msmpeg4v3"), ("mp3", "ac3", "pcm_s16le")),
    "GIF": Container("gif", ".gif"),
    "MP3": Container("mp3", ".mp3", audio=("mp3",)),
    "M4A": Container("ipod", ".m4a", audio=("aac", "alac")),
    "WAV": Container("wav", ".wav", audio=("pcm_s16le", "pcm_s24le", "pcm_f32le")),
    "FLAC": Container("flac", ".flac", audio=("flac",)),
    "OGG": Container("ogg", ".ogg", audio=("vorbis", "opus", "flac")),
    "OPUS": Container("opus", ".opus", audio=("opus",)),
    "AIFF": Container("aiff", ".aiff", audio=("pcm_s16be", "pcm_s24be")),
}
AUDIO_ONLY = set(AUDIO_TARGETS)

# what a source extension "is", for tools that keep the format (anything else becomes MP4 / M4A)
KEEP = {".mp4": "MP4", ".m4v": "MP4", ".mov": "MOV", ".mkv": "MKV", ".webm": "WEBM", ".avi": "AVI",
        ".mp3": "MP3", ".m4a": "M4A", ".aac": "M4A", ".wav": "WAV", ".flac": "FLAC", ".ogg": "OGG",
        ".oga": "OGG", ".opus": "OPUS", ".aiff": "AIFF", ".aif": "AIFF"}
SAME = {"m4v": "MP4", "aif": "AIFF", "oga": "OGG"}


def targets_for(cat: str, src_ext: str) -> list[str]:
    ext = src_ext.lower().lstrip(".")
    cur = SAME.get(ext, ext.upper())
    targets = VIDEO_TARGETS if cat == "video" else AUDIO_TARGETS if cat == "audio" else []
    return [t for t in targets if t != cur]


def _kind(info: Info) -> str:
    return "video" if info.video else "audio"


def _keep_target(src: Path, info: Info) -> tuple[str, str]:
    """(target, extension) for tools that keep the source's format where they can."""
    target = KEEP.get(src.suffix.lower())
    if target is None or (target in AUDIO_ONLY) != (_kind(info) == "audio"):
        target = "MP4" if _kind(info) == "video" else "M4A"
    ext = src.suffix.lower() if KEEP.get(src.suffix.lower()) == target else CONTAINERS[target].ext
    return target, ext


def _require_video(info: Info) -> Stream:
    if not info.video:
        raise UserError("This file has no video, only sound.")
    return info.video[0]


def _require_audio(info: Info) -> Stream:
    if not info.audio:
        raise UserError("This video has no sound to extract.")
    return info.audio[0]


def _video_bitrate(info: Info, crf: int) -> str:
    """Bitrate for encoders without a quality mode (hardware H.264): about 0.1 bits per pixel
    at CRF 23, halving every 6 CRF steps, never above the source's own rate."""
    w, h = info.size or (1280, 720)
    fps = info.video[0].fps or 30
    rate = w * h * fps * 0.1 * 2 ** ((23 - crf) / 6)
    src_rate = info.video[0].bit_rate or info.bit_rate
    if src_rate and crf >= CRF_COMPRESS:
        rate = min(rate, src_rate * 0.6)
    return str(int(min(max(rate, 200_000), 40_000_000)))


def _rate(bitrate: int) -> list[str]:
    """A target bitrate (bits/s) with a buffer, so one pass lands close to the planned size."""
    return ["-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.5)), "-bufsize", str(bitrate * 2)]


def _h264(info: Info, crf: int, bitrate: int | None = None) -> list[str]:
    if has_encoder("libx264"):
        quality = _rate(bitrate) if bitrate else ["-crf", str(crf)]
        return ["-c:v", "libx264", "-preset", "medium", *quality, "-pix_fmt", "yuv420p"]
    # LGPL ffmpeg builds have no x264: use the platform's encoder; ffmpeg picks its pixel format
    for enc in ("h264_videotoolbox", "h264_mf", "libopenh264"):
        if _usable_video_encoder(enc):
            # OpenH264's default "quality" mode treats -b:v loosely (a 3 s clip asked for 30% came
            # out at 47%); bitrate mode holds it
            rc = ["-rc_mode", "bitrate"] if enc == "libopenh264" else []
            return ["-c:v", enc, *rc, "-b:v", str(bitrate) if bitrate else _video_bitrate(info, crf)]
    # MPEG-4 Part 2 is always built in and is valid in MP4, MOV and MKV
    quality = _rate(bitrate) if bitrate else ["-q:v", "3" if crf < CRF_COMPRESS else "6"]
    return ["-c:v", "mpeg4", *quality, "-pix_fmt", "yuv420p"]


def _video_codec(target: str, info: Info, crf: int, bitrate: int | None = None) -> list[str]:
    """Encoder options; `bitrate` (bits/s) replaces the quality setting with a size target."""
    if target == "WEBM":
        vp9_crf = VP9_CRF_COMPRESS if crf >= CRF_COMPRESS else VP9_CRF_CONVERT
        quality = _rate(bitrate) if bitrate else ["-crf", str(vp9_crf), "-b:v", "0"]
        return ["-c:v", _need("libvpx-vp9", "WebM video"), *quality,
                "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p"]
    if target == "AVI":
        quality = _rate(bitrate) if bitrate else ["-q:v", "4"]
        return ["-c:v", "mpeg4", "-vtag", "XVID", *quality, "-pix_fmt", "yuv420p"]
    return _h264(info, crf, bitrate)


def _video_args(target: str, info: Info, vf: list[str], crf: int, copy: bool,
                bitrate: int | None = None) -> list[str]:
    v = info.video[0]
    if copy and not vf and v.codec in CONTAINERS[target].video:
        tag = ["-tag:v", "hvc1"] if v.codec == "hevc" and target in ("MP4", "MOV") else []  # Apple players
        return ["-c:v", "copy", *tag]
    # 4:2:0 video needs even dimensions; this is a no-op when they already are
    filters = [*vf, "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    return ["-vf", ",".join(filters), *_video_codec(target, info, crf, bitrate)]


def _pcm(bits: int, endian: str) -> str:
    return f"pcm_s{24 if bits > 16 else 16}{endian}"


def _audio_codec(target: str, a: Stream, kbps: int | None) -> list[str]:
    """Encoder options for `target`. `kbps` overrides the default bitrate for lossy codecs."""
    if target in ("MP4", "MOV", "MKV", "M4A"):
        return ["-c:a", "aac", "-b:a", f"{kbps or 192}k"]
    if target in ("WEBM", "OPUS"):
        return ["-c:a", _need("libopus", "Opus audio"), "-b:a", f"{kbps or 128}k"]
    if target in ("MP3", "AVI"):
        if target == "AVI" and not has_encoder("libmp3lame"):
            return ["-c:a", "ac3", "-b:a", f"{kbps or 192}k"]
        rate = ["-b:a", f"{kbps}k"] if kbps else ["-q:a", "2"]   # -q:a 2 is LAME's ~190 kbps VBR
        stereo = ["-ac", "2"] if a.channels > 2 else []          # MP3 has no surround
        return ["-c:a", _need("libmp3lame", "MP3 audio"), *rate, *stereo]
    if target == "OGG":
        if has_encoder("libvorbis"):
            return ["-c:a", "libvorbis", "-b:a", f"{kbps}k"] if kbps else ["-c:a", "libvorbis", "-q:a", "5"]
        return ["-c:a", _need("libopus", "OGG audio"), "-b:a", f"{kbps or 128}k"]   # Opus in Ogg
    if target == "WAV":
        return ["-c:a", _pcm(a.bits, "le")]
    if target == "AIFF":
        return ["-c:a", _pcm(a.bits, "be")]
    if target == "FLAC":
        return ["-c:a", "flac"]
    raise ValueError(f"No audio codec for {target}")


def _audio_args(target: str, streams: list[Stream], af: list[str], kbps: int | None, copy: bool) -> list[str]:
    allowed = CONTAINERS[target].audio
    if copy and not af and not kbps and all(s.codec in allowed for s in streams):
        return ["-c:a", "copy"]
    args = _audio_codec(target, streams[0], kbps)
    if af:
        args += ["-af", ",".join(af)]
        # filters like loudnorm resample to 192 kHz; go back to the source rate (Opus only does 48 kHz)
        opus = "libopus" in args
        rate = 48000 if opus else streams[0].sample_rate
        if rate:
            args += ["-ar", str(rate)]
    return args


def _transcode(src: Path, info: Info, target: str, out: Path, *, vf: list[str] | None = None,
               af: list[str] | None = None, crf: int = CRF_CONVERT, kbps: int | None = None,
               copy: bool = True, mute: bool = False, start: float | None = None,
               length: float | None = None, extra: list[str] | None = None,
               duration: float | None = None, bitrate: int | None = None) -> Path:
    """One ffmpeg pass from `src` to `target`. Streams are copied when they already fit the
    container and nothing (filters, bitrate, trimming) asks for a re-encode."""
    vf, af = vf or [], af or []
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if length is not None:
        args += ["-t", f"{length:.3f}"]
    args += ["-i", _file(src)]
    if target in AUDIO_ONLY:
        a = _require_audio(info)
        args += ["-map", f"0:{a.index}", "-vn", "-sn", "-dn"]
        args += _audio_args(target, [a], af, kbps, copy)
    else:
        v = _require_video(info)
        args += ["-map", f"0:{v.index}", "-sn", "-dn"]
        args += _video_args(target, info, vf, crf, copy and not bitrate, bitrate)
        if mute or not info.audio:
            args += ["-an"]
        else:
            args += ["-map", "0:a"]
            args += _audio_args(target, info.audio, af, kbps, copy)
        if target in ("MP4", "MOV"):
            args += ["-movflags", "+faststart"]   # starts playing before it's fully downloaded
    args += extra or []
    if duration is None:
        duration = length if length is not None else info.duration
    return run(args, out, CONTAINERS[target].muxer, duration)


def _gif(src: Path, info: Info, out: Path) -> Path:
    v = _require_video(info)
    if info.duration > GIF_MAX_SECONDS:
        raise UserError(f"This clip is {fmt_duration(info.duration)} long. GIFs work best under "
                        f"{GIF_MAX_SECONDS} seconds, so trim it first.")
    fps = min(GIF_FPS, round(v.fps)) if v.fps else GIF_FPS
    # one pass: build a 256-colour palette from the clip, then map every frame onto it
    graph = (f"[0:{v.index}]fps={fps},scale='min({GIF_WIDTH},iw)':-2:flags=lanczos,split[a][b];"
             f"[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4[out]")
    args = ["-i", _file(src), "-filter_complex", graph, "-map", "[out]", "-an", "-loop", "0"]
    return run(args, out, "gif", info.duration)


def convert(src: str | Path, target: str) -> Path:
    src = Path(src)
    target = target.upper()
    if target not in CONTAINERS:
        raise ValueError(f"Can't convert media to {target}")
    info = probe(src)
    out = sibling(src, ext=CONTAINERS[target].ext)
    if target == "GIF":
        return _gif(src, info, out)
    return _transcode(src, info, target, out)


# ---------------------------------------------------------------- tools
def _budget(src: Path, info: Info, percent: int) -> float:
    """Bits per second that make the whole file about `percent`% of its size."""
    if info.duration <= 0:
        raise UserError("DragShift can't tell how long this file is, so it can't aim for a size.")
    return file_size(src) * percent / 100 * 8 / info.duration


def compress(src: str | Path, percent: int | None = None) -> Path:
    """Smaller video at the same resolution: H.264 CRF 28 (VP9 for WebM) and AAC 128 kbps, or,
    with `percent`, a bitrate planned to land near that share of the original size."""
    src = Path(src)
    info = probe(src)
    if not info.video:
        return compress_audio(src, percent)
    target, ext = _keep_target(src, info)
    if target == "AVI":
        target, ext = "MP4", ".mp4"
    out = sibling(src, "Compressed", ext)
    if percent is None:
        return _transcode(src, info, target, out, crf=CRF_COMPRESS, kbps=128, copy=False)
    budget = _budget(src, info, percent)
    kbps = max(48, min(128, int(budget * 0.1 / 1000))) if info.audio else None
    video = int(budget * PERCENT_MUX_SHARE - (kbps or 0) * 1000)
    if video < MIN_VIDEO_BITRATE:
        raise UserError(f"{percent}% is too small for this video. Try a higher percentage.")
    return _transcode(src, info, target, out, kbps=kbps, copy=False, bitrate=video)


def compress_audio(src: str | Path, percent: int | None = None) -> Path:
    """Lossy files keep their format at about half the bitrate (64 to 128 kbps); lossless ones
    become 128 kbps AAC (.m4a). With `percent`, the bitrate is that share of the original's."""
    src = Path(src)
    info = probe(src)
    a = _require_audio(info)
    target, ext = _keep_target(src, info)
    if a.codec not in LOSSY_AUDIO or target in ("WAV", "FLAC", "AIFF") or target not in AUDIO_ONLY:
        target, ext = "M4A", ".m4a"
    src_kbps = (a.bit_rate or info.bit_rate) // 1000
    floor = 48 if target == "OPUS" else 64
    if percent is None:
        kbps = max(floor, min(128, src_kbps // 2)) if src_kbps else 96
    else:
        planned = src_kbps * percent / 100 if src_kbps else _budget(src, info, percent) / 1000
        kbps = int(max(32, min(320, planned)))
    return _transcode(src, info, target, sibling(src, "Compressed", ext), kbps=kbps, copy=False)


EXTRACT = {"aac": "M4A", "alac": "M4A", "mp3": "MP3", "opus": "OPUS", "vorbis": "OGG", "flac": "FLAC",
           "pcm_s16le": "WAV", "pcm_s24le": "WAV"}


def extract_audio(src: str | Path) -> Path:
    """The soundtrack as its own file, copied without re-encoding when the codec has a natural
    home (AAC to .m4a, MP3 to .mp3, Opus to .opus...); anything else becomes AAC."""
    src = Path(src)
    info = probe(src)
    a = _require_audio(info)
    target = EXTRACT.get(a.codec, "M4A")
    return _transcode(src, info, target, sibling(src, ext=CONTAINERS[target].ext))


def mute(src: str | Path) -> Path:
    src = Path(src)
    info = probe(src)
    _require_video(info)
    target, ext = _keep_target(src, info)
    return _transcode(src, info, target, sibling(src, "Muted", ext), mute=True)


def frame_grab(src: str | Path, at: float | None = None) -> Path:
    """One frame as PNG: at `at` seconds, or the middle of the clip."""
    src = Path(src)
    info = probe(src)
    v = _require_video(info)
    if at is None:
        at = info.duration / 2
    at = min(max(0.0, at), max(0.0, info.duration - 0.05)) if info.duration else 0.0
    args = ["-ss", f"{at:.3f}", "-i", _file(src), "-map", f"0:{v.index}", "-frames:v", "1",
            "-update", "1", "-c:v", "png"]
    return run(args, sibling(src, "Frame", ".png"), "image2")


# ---------------------------------------------------------------- previews for tool windows
# These return image bytes instead of writing a file, so scrubbing never leaves anything beside
# the user's file. They run on worker threads; the windows never call them on the UI thread.
def _pipe(args: list[str], timeout: float = 120, cancel: threading.Event | None = None) -> bytes:
    """Run ffmpeg with output on stdout (`args` end with the muxer options) and return it.
    Setting `cancel` (or the job's, see `reporting`) kills ffmpeg and raises Cancelled."""
    cmd = [_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", *args, "pipe:1"]
    cancel = cancel or hooks()[1]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_quiet())
    except OSError as e:
        _log(f"ffmpeg could not start: {e}")
        raise UserError(MISSING) from e
    try:
        out, err = _wait(proc, proc.communicate, cancel, timeout)
    except subprocess.TimeoutExpired as e:
        raise UserError(UNREADABLE) from e
    if proc.returncode != 0 or not out:
        err = err.decode("utf-8", "replace")
        _log(f"ffmpeg preview failed (exit {proc.returncode}): {_last_line(err)}\n  command: {cmd}")
        raise UserError(_explain(err.splitlines()[-12:]))
    return out


def preview_frame(src: str | Path, at: float, size: tuple[int, int], index: int = 0,
                  exact: bool = True) -> bytes:
    """One frame of stream `index` at `at` seconds as PNG bytes, scaled to `size` (display
    orientation: ffmpeg applies rotation metadata first). `exact=False` shows the keyframe
    before `at`, which is much faster for thumbnails."""
    w, h = size
    seek = [] if exact else ["-noaccurate_seek"]
    return _pipe([*seek, "-ss", f"{max(0.0, at):.3f}", "-i", _file(src), "-map", f"0:{index}",
                  "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "image2pipe", "-c:v", "png"])


def waveform(src: str | Path, size: tuple[int, int], index: int = 0,
             cancel: threading.Event | None = None) -> bytes:
    """A white-on-transparent waveform of audio stream `index` as PNG bytes. The square-root
    scale keeps quiet passages visible. It reads the whole file, which takes a while for a long
    recording, so it stops (raising Cancelled) as soon as `cancel` is set."""
    w, h = size
    graph = f"[0:{index}]aformat=channel_layouts=mono,showwavespic=s={w}x{h}:colors=white:scale=sqrt"
    return _pipe(["-i", _file(src), "-filter_complex", graph, "-frames:v", "1", "-f", "image2pipe",
                  "-c:v", "png"], cancel=cancel)


def rotate(src: str | Path, clockwise: bool = True) -> Path:
    src = Path(src)
    info = probe(src)
    _require_video(info)
    target, ext = _keep_target(src, info)
    return _transcode(src, info, target, sibling(src, "Rotated", ext),
                      vf=["transpose=1" if clockwise else "transpose=2"], copy=False)


def trim_range(start: float, end: float, duration: float) -> tuple[float, float]:
    """The (start, end) a trim will really use: `end` is clamped to the clip. Raises UserError
    when the start is past the end of the clip or the range is empty or reversed."""
    if duration and start >= duration:
        raise UserError("The start point is past the end of the clip.")
    if duration:
        end = min(end, duration)
    if start < 0 or end - start < MIN_TRIM:
        raise UserError("Pick a start point before the end point.")
    return start, end


def trim(src: str | Path, start: float, end: float, fast: bool = False) -> Path:
    """Keep `start`..`end` seconds. By default it re-encodes so the cut lands on the exact frame;
    `fast` copies the streams instead (no quality loss, but video can only start on a keyframe,
    so the cut may begin a little early)."""
    src = Path(src)
    info = probe(src)
    start, end = trim_range(start, end, info.duration)
    target, ext = _keep_target(src, info)
    return _transcode(src, info, target, sibling(src, "Trimmed", ext), start=start, length=end - start,
                      copy=fast)


def crop(src: str | Path, rect: tuple[int, int, int, int]) -> Path:
    """Crop to `rect` = (x, y, width, height) in display pixels (after any rotation)."""
    src = Path(src)
    info = probe(src)
    _require_video(info)
    x, y, w, h = (int(round(n)) for n in rect)
    w, h = w - w % 2, h - h % 2
    sw, sh = info.size or (0, 0)
    if x < 0 or y < 0 or w < 2 or h < 2 or (sw and x + w > sw) or (sh and y + h > sh):
        raise UserError("The crop area is outside the video.")
    target, ext = _keep_target(src, info)
    return _transcode(src, info, target, sibling(src, "Cropped", ext), vf=[f"crop={w}:{h}:{x}:{y}"],
                      copy=False)


def _atempo(factor: float) -> list[str]:
    """atempo only takes 0.5-2.0 on older ffmpeg, so bigger changes are chained."""
    steps = []
    while factor > 2.0:
        steps.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        steps.append(0.5)
        factor /= 0.5
    steps.append(factor)
    return [f"atempo={s:.6g}" for s in steps]


def _pitch_shift(factor: float, rate: int) -> list[str]:
    """Faster and higher (or slower and lower), like a tape played at another speed: relabel the
    sample rate, then resample back so players see the original rate."""
    return [f"asetrate={round(rate * factor)}", f"aresample={rate}"]


def speed(src: str | Path, factor: float, keep_pitch: bool = True) -> Path:
    """Play `factor` times faster (0.5 = half speed). Sound keeps its pitch unless `keep_pitch`
    is off."""
    lo, hi = SPEED_RANGE
    if not lo <= factor <= hi:
        raise UserError(f"Speed must be between {lo:g}x and {hi:g}x.")
    src = Path(src)
    info = probe(src)
    target, ext = _keep_target(src, info)
    vf = [f"setpts=PTS/{factor:.6g}"] if info.video else []
    rate = info.audio[0].sample_rate if info.audio else 0
    af = _atempo(factor) if keep_pitch or not rate else _pitch_shift(factor, rate)
    return _transcode(src, info, target, sibling(src, f"{factor:g}x", ext), vf=vf, af=af,
                      copy=False, duration=info.duration / factor)


def normalize(src: str | Path) -> Path:
    """Even out loudness to -16 LUFS (single-pass loudnorm), same format where possible."""
    src = Path(src)
    info = probe(src)
    _require_audio(info)
    target, ext = _keep_target(src, info)
    return _transcode(src, info, target, sibling(src, "Normalized", ext), af=[LOUDNORM], copy=False)


def strip_metadata(src: str | Path) -> Path:
    """Copy without tags, chapters or cover art (audio is not re-encoded when it fits)."""
    src = Path(src)
    info = probe(src)
    _require_audio(info)
    target, ext = _keep_target(src, info)
    # bitexact stops ffmpeg from writing its own "encoder" tag
    return _transcode(src, info, target, sibling(src, "Clean", ext),
                      extra=["-map_metadata", "-1", "-map_metadata:s:a", "-1", "-map_chapters", "-1",
                             "-fflags", "+bitexact"])
