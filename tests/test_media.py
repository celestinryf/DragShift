"""Video and audio (dragshift/media.py). Clips are generated with ffmpeg's lavfi sources in a temp
folder; tests that need ffmpeg skip cleanly when it isn't installed. Results are checked with a
direct ffprobe call (not media.probe), so the assertions don't trust the code under test."""
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from dragshift import convert, media, report
from dragshift.util import UserError, category

needs_ffmpeg = pytest.mark.skipif(not media.available(), reason="ffmpeg/ffprobe not installed")
DUR_TOL = 0.15   # seconds; containers pad a frame or an audio packet


# ---------------------------------------------------------------- helpers
def ffprobe(path: Path) -> dict:
    out = subprocess.run([media.find_tool("ffprobe"), "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", "-count_frames", "file:" + str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def streams(path: Path, kind: str) -> list[dict]:
    return [s for s in ffprobe(path)["streams"] if s["codec_type"] == kind
            and not s.get("disposition", {}).get("attached_pic")]


def vcodec(path):
    s = streams(path, "video")
    return s[0]["codec_name"] if s else None


def acodec(path):
    s = streams(path, "audio")
    return s[0]["codec_name"] if s else None


def duration(path: Path) -> float:
    return float(ffprobe(path)["format"]["duration"])


def size(path: Path) -> tuple[int, int]:
    v = streams(path, "video")[0]
    return v["width"], v["height"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def h264_or_fallback() -> str:
    """What media picks for H.264 on this ffmpeg: h264, or MPEG-4 Part 2 if no encoder works."""
    return "h264" if media._h264(media.Info(1.0, "", video=[media.Stream(0, "video", "x", 160, 120, 25)]),
                                 media.CRF_CONVERT)[1] != "mpeg4" else "mpeg4"


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """Read-only sources shared by tests (tests copy them before renaming)."""
    if not media.available():
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("clips")
    return {
        "mp4": media.make_test_clip(d / "clip.mp4"),                                    # mpeg4 + aac
        "silent": media.make_test_clip(d / "silent.mp4", audio=False),
        "webm": media.make_test_clip(d / "clip.webm", vcodec="libvpx-vp9", acodec="libopus", muxer="webm")
        if media.has_encoder("libvpx-vp9") and media.has_encoder("libopus") else None,
        "odd": media.make_test_clip(d / "odd.mkv", size="161x121", vcodec="ffv1", muxer="matroska"),
        "wav": media.make_test_clip(d / "tone.wav", video=False, acodec="pcm_s16le", muxer="wav"),
        "flac24": media.make_test_clip(d / "tone24.flac", video=False, acodec="flac", muxer="flac",
                                       extra=["-sample_fmt", "s32"]),
        "tagged": media.make_test_clip(d / "tagged.m4a", video=False, acodec="aac", muxer="ipod",
                                       extra=["-metadata", "title=Secret Title", "-metadata", "artist=Me"]),
    }


@pytest.fixture
def clip(clips, tmp_path) -> Path:
    """A private copy of the mpeg4 + aac clip in an empty folder."""
    p = tmp_path / "clip.mp4"
    p.write_bytes(clips["mp4"].read_bytes())
    return p


def only_files(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()}


# ---------------------------------------------------------------- categories and menus
def test_category_media():
    for ext in ("mp4", "M4V", "mov", "mkv", "webm", "avi", "wmv", "flv", "mpg", "mpeg", "3gp", "MTS"):
        assert category(f"x.{ext}") == "video", ext
    for ext in ("mp3", "m4a", "AAC", "wav", "flac", "ogg", "oga", "opus", "aiff", "aif", "wma", "alac", "caf"):
        assert category(f"x.{ext}") == "audio", ext
    assert category("x.gif") == "image"
    assert category("x.mp5") is None


def test_ts_is_video_only_when_it_is_a_transport_stream(tmp_path):
    stream = tmp_path / "clip.ts"
    stream.write_bytes((b"\x47" + bytes(187)) * 4)
    script = tmp_path / "app.ts"
    script.write_text("export const answer: number = 42;\n" * 10)
    tiny = tmp_path / "tiny.ts"
    tiny.write_bytes(b"\x47")
    assert category(stream) == "video"
    assert category(script) == "text"
    assert category(tiny) == "text"
    assert category(tmp_path / "missing.ts") == "text"


def test_targets_for():
    assert convert.targets_for("video", ".mp4") == ["MOV", "MKV", "WEBM", "AVI", "GIF", "MP3", "M4A", "WAV"]
    assert "MP4" not in convert.targets_for("video", ".m4v")
    assert convert.targets_for("video", ".wmv") == media.VIDEO_TARGETS
    assert convert.targets_for("audio", ".mp3") == ["M4A", "WAV", "FLAC", "OGG", "OPUS", "AIFF"]
    assert "AIFF" not in convert.targets_for("audio", ".aif")
    assert "OGG" not in convert.targets_for("audio", ".oga")
    assert convert.targets_for("audio", ".aac") == media.AUDIO_TARGETS   # raw AAC becomes M4A
    for t in media.VIDEO_TARGETS + media.AUDIO_TARGETS:
        assert t in media.CONTAINERS


def test_menu_entries():
    from dragshift import app
    for cat, tools in app.MEDIA_TOOLS.items():
        items = app.tool_items(list(tools), cat)
        assert [i.key for i in items] == [f"tool:{n}" for n in tools]
        assert all(i.glyph and i.hint for i in items)
    assert app.tool_items(["Rotate"], "video")[0].hint == "Turn 90° clockwise"
    assert app.tool_items(["Rotate"], "pdf")[0].hint == "Turn pages 90°"
    for t in media.VIDEO_TARGETS + media.AUDIO_TARGETS:
        assert t in app.FORMAT_HINTS


# ---------------------------------------------------------------- finding ffmpeg
def test_missing_ffmpeg(tmp_path, monkeypatch):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"not really a video")
    monkeypatch.setattr(media, "find_tool", lambda name: None)
    assert not media.available()
    for call in (lambda: media.probe(f), lambda: convert.convert(f, "video", "MKV"), lambda: media.compress(f)):
        with pytest.raises(UserError, match="need ffmpeg"):
            call()
    assert only_files(tmp_path) == {"x.mp4"}


def test_missing_ffprobe_only(tmp_path, monkeypatch):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    real = media.find_tool
    monkeypatch.setattr(media, "find_tool", lambda name: None if name == "ffprobe" else real(name))
    with pytest.raises(UserError, match="need ffmpeg"):
        media.convert(f, "MKV")


def test_ffmpeg_that_cannot_start(tmp_path, monkeypatch):
    """A path that exists in the cache but can't be executed gives the same plain message."""
    monkeypatch.setattr(media, "find_tool", lambda name: str(tmp_path / "gone" / name))
    with pytest.raises(UserError, match="need ffmpeg"):
        media.run(["-f", "lavfi", "-i", "anullsrc"], tmp_path / "o.wav", "wav")


def test_bundled_ffmpeg_wins(tmp_path, monkeypatch):
    exe = ".exe" if sys.platform == "win32" else ""
    (tmp_path / "ffmpeg").mkdir()
    fake = tmp_path / "ffmpeg" / f"ffprobe{exe}"
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    media.find_tool.cache_clear()
    try:
        assert media.find_tool("ffprobe") == str(fake)
    finally:
        media.find_tool.cache_clear()


# ---------------------------------------------------------------- conversions
VIDEO_EXPECT = {   # target -> (video codec, audio codec) for the mpeg4 + aac source
    "MOV": ("mpeg4", "aac"),       # both copied
    "MKV": ("mpeg4", "aac"),       # both copied
    "WEBM": ("vp9", "opus"),
    "AVI": ("mpeg4", "mp3"),       # video copied, AAC isn't AVI-friendly
    "MP3": (None, "mp3"),
    "M4A": (None, "aac"),          # copied
    "WAV": (None, "pcm_s16le"),    # lossy source -> 16-bit
}


@needs_ffmpeg
@pytest.mark.parametrize("target", convert.targets_for("video", ".mp4"))
def test_video_conversions(clip, target):
    before = digest(clip)
    out = convert.convert(clip, "video", target)[0]
    assert out != clip and out.parent == clip.parent
    assert out.suffix == media.CONTAINERS[target].ext and out.stem == "clip"
    assert digest(clip) == before, "the original must never change"
    assert only_files(clip.parent) == {"clip.mp4", out.name}, "no temp files left behind"
    if target == "GIF":
        v = streams(out, "video")[0]
        assert v["codec_name"] == "gif" and int(v["nb_read_frames"]) > 1
        assert v["width"] <= media.GIF_WIDTH and acodec(out) is None
        with Image.open(out) as im:
            assert im.is_animated and im.n_frames > 1
        return
    want_v, want_a = VIDEO_EXPECT[target]
    if target == "WEBM" and not media.has_encoder("libvpx-vp9"):
        pytest.skip("no VP9 encoder")
    assert vcodec(out) == want_v and acodec(out) == want_a
    assert abs(duration(out) - 1.0) < DUR_TOL
    if want_v:
        assert size(out) == (160, 120)


@needs_ffmpeg
def test_reencode_to_mp4(clips, tmp_path):
    if clips["webm"] is None:
        pytest.skip("no VP9/Opus encoders to make the source")
    src = tmp_path / "clip.webm"
    src.write_bytes(clips["webm"].read_bytes())
    out = convert.convert(src, "video", "MP4")[0]
    assert vcodec(out) == h264_or_fallback() and acodec(out) == "aac"
    assert streams(out, "video")[0]["pix_fmt"] == "yuv420p"
    data = out.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat"), "+faststart puts the index first"
    assert abs(duration(out) - 1.0) < DUR_TOL


@needs_ffmpeg
def test_odd_dimensions_become_even(clips, tmp_path):
    src = tmp_path / "odd.mkv"
    src.write_bytes(clips["odd"].read_bytes())
    out = convert.convert(src, "video", "MP4")[0]
    assert size(out) == (160, 120)
    assert size(convert.convert(src, "video", "MOV")[0]) == (160, 120)


@needs_ffmpeg
def test_stream_copy_into_mp4(clips, tmp_path):
    """MPEG-4 Part 2 + AAC already fit MP4, so MOV -> MP4 copies instead of re-encoding."""
    src = tmp_path / "clip.mp4"
    src.write_bytes(clips["mp4"].read_bytes())
    mov = convert.convert(src, "video", "MOV")[0]
    out = convert.convert(mov, "video", "MP4")[0]
    assert out.name == "clip (1).mp4"
    assert vcodec(out) == "mpeg4" and acodec(out) == "aac"


@needs_ffmpeg
@pytest.mark.parametrize("target", convert.targets_for("audio", ".wav"))
def test_audio_conversions(clips, tmp_path, target):
    src = tmp_path / "tone.wav"
    src.write_bytes(clips["wav"].read_bytes())
    out = convert.convert(src, "audio", target)[0]
    want = {"MP3": {"mp3"}, "M4A": {"aac"}, "FLAC": {"flac"}, "OGG": {"vorbis", "opus"}, "OPUS": {"opus"},
            "AIFF": {"pcm_s16be"}}[target]
    assert acodec(out) in want and vcodec(out) is None
    assert abs(duration(out) - 1.0) < DUR_TOL
    assert out.suffix == media.CONTAINERS[target].ext


@needs_ffmpeg
def test_bit_depth_is_kept(clips, tmp_path):
    src = tmp_path / "tone24.flac"
    src.write_bytes(clips["flac24"].read_bytes())
    assert acodec(convert.convert(src, "audio", "WAV")[0]) == "pcm_s24le"
    assert acodec(convert.convert(src, "audio", "AIFF")[0]) == "pcm_s24be"


@needs_ffmpeg
def test_video_without_sound(clips, tmp_path):
    src = tmp_path / "silent.mp4"
    src.write_bytes(clips["silent"].read_bytes())
    for target in ("MP3", "M4A", "WAV"):
        with pytest.raises(UserError, match="no sound"):
            convert.convert(src, "video", target)
    with pytest.raises(UserError, match="no sound"):
        media.extract_audio(src)
    assert only_files(tmp_path) == {"silent.mp4"}
    out = convert.convert(src, "video", "MKV")[0]
    assert acodec(out) is None and vcodec(out) == "mpeg4"


@needs_ffmpeg
def test_audio_file_has_no_video(clips, tmp_path):
    src = tmp_path / "tone.wav"
    src.write_bytes(clips["wav"].read_bytes())
    with pytest.raises(UserError, match="no video"):
        media.convert(src, "MP4")


@needs_ffmpeg
def test_gif_length_guard(clip, monkeypatch):
    monkeypatch.setattr(media, "GIF_MAX_SECONDS", 0.5)
    with pytest.raises(UserError, match="trim it first"):
        convert.convert(clip, "video", "GIF")
    assert only_files(clip.parent) == {"clip.mp4"}


@needs_ffmpeg
def test_corrupt_input(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40)
    with pytest.raises(UserError, match="damaged"):
        convert.convert(bad, "video", "MKV")
    with pytest.raises(UserError, match="damaged"):
        media.compress(bad)
    assert only_files(tmp_path) == {"broken.mp4"}


@needs_ffmpeg
def test_missing_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        media.probe(tmp_path / "nope.mp4")


NASTY = ["-rf.mp4", "-i.mp4", "it's \"quoted\".mp4", "vidéo 日本 ñ.mp4", "a b; rm -rf ~.mp4", "$(touch x).mp4"]
if sys.platform != "win32":
    NASTY += ["http:x.mp4", "concat:a|b.mp4"]


@needs_ffmpeg
@pytest.mark.parametrize("name", NASTY)
def test_unsafe_file_names(clips, tmp_path, name):
    src = tmp_path / name
    src.write_bytes(clips["mp4"].read_bytes())
    before = digest(src)
    mkv = convert.convert(src, "video", "MKV")[0]
    mp3 = convert.convert(src, "video", "MP3")[0]
    assert mkv.name == Path(name).stem + ".mkv" and mp3.name == Path(name).stem + ".mp3"
    assert vcodec(mkv) == "mpeg4" and acodec(mp3) == "mp3"
    assert digest(src) == before
    assert only_files(tmp_path) == {name, mkv.name, mp3.name}


@needs_ffmpeg
def test_never_overwrites(clip):
    existing = clip.with_suffix(".mkv")
    existing.write_bytes(b"keep me")
    out = convert.convert(clip, "video", "MKV")[0]
    assert out.name == "clip (1).mkv" and existing.read_bytes() == b"keep me"
    again = convert.convert(clip, "video", "MKV")[0]
    assert again.name == "clip (2).mkv"


# ---------------------------------------------------------------- tools
@needs_ffmpeg
def test_video_compress(clip):
    out = media.compress(clip)
    assert out.name == "clip Compressed.mp4"
    assert vcodec(out) == h264_or_fallback() and acodec(out) == "aac"
    assert size(out) == (160, 120) and abs(duration(out) - 1.0) < DUR_TOL


@needs_ffmpeg
def test_extract_audio_copies(clip):
    out = media.extract_audio(clip)
    assert out.suffix == ".m4a" and acodec(out) == "aac" and vcodec(out) is None
    src_audio = streams(clip, "audio")[0]
    assert streams(out, "audio")[0]["bit_rate"] == src_audio["bit_rate"], "copied, not re-encoded"


@needs_ffmpeg
def test_mute(clip):
    out = media.mute(clip)
    assert out.name == "clip Muted.mp4"
    assert acodec(out) is None and vcodec(out) == "mpeg4"


@needs_ffmpeg
def test_frame_grab(clip):
    out = media.frame_grab(clip)
    assert out.name == "clip Frame.png"
    with Image.open(out) as im:
        assert im.format == "PNG" and im.size == (160, 120)
    late = media.frame_grab(clip, at=99)   # clamped to the last frame, not an empty file
    assert late.stat().st_size > 0


@needs_ffmpeg
def test_rotate(clip):
    out = media.rotate(clip)
    assert out.name == "clip Rotated.mp4" and size(out) == (120, 160)
    assert acodec(out) == "aac"
    assert size(media.rotate(clip, clockwise=False)) == (120, 160)


@needs_ffmpeg
def test_trim(clip):
    out = media.trim(clip, 0.25, 0.75)
    assert out.name == "clip Trimmed.mp4"
    assert abs(duration(out) - 0.5) < DUR_TOL
    assert abs(duration(media.trim(clip, 0.5, 50)) - 0.5) < DUR_TOL   # end clamped to the clip
    for start, end in ((0.6, 0.5), (-1, 0.5), (0.3, 0.3)):
        with pytest.raises(UserError):
            media.trim(clip, start, end)
    with pytest.raises(UserError, match="past the end"):
        media.trim(clip, 5, 6)


@needs_ffmpeg
def test_trim_audio(clips, tmp_path):
    src = tmp_path / "tone.wav"
    src.write_bytes(clips["wav"].read_bytes())
    out = media.trim(src, 0, 0.4)
    assert out.suffix == ".wav" and acodec(out) == "pcm_s16le"
    assert abs(duration(out) - 0.4) < 0.05


@needs_ffmpeg
def test_crop(clip):
    out = media.crop(clip, (10, 20, 81, 61))   # odd sizes are rounded down to even
    assert out.name == "clip Cropped.mp4" and size(out) == (80, 60)
    for rect in ((150, 0, 40, 40), (-1, 0, 20, 20), (0, 0, 1, 1), (0, 100, 160, 40)):
        with pytest.raises(UserError, match="outside"):
            media.crop(clip, rect)


@needs_ffmpeg
@pytest.mark.parametrize("factor", [2.0, 0.5])
def test_speed(clip, factor):
    out = media.speed(clip, factor)
    assert out.name == f"clip {factor:g}x.mp4"
    assert abs(duration(out) - 1.0 / factor) < DUR_TOL * 2
    assert acodec(out) == "aac"


def test_speed_limits_and_atempo(tmp_path):
    for bad in (0.1, 5, 0):
        with pytest.raises(UserError, match="between"):
            media.speed(tmp_path / "x.mp4", bad)   # checked before ffmpeg is even needed
    assert media._atempo(4.0) == ["atempo=2", "atempo=2"]
    assert media._atempo(0.25) == ["atempo=0.5", "atempo=0.5"]
    assert media._atempo(1.5) == ["atempo=1.5"]


@needs_ffmpeg
def test_audio_compress(clips, tmp_path):
    src = tmp_path / "tone.wav"
    src.write_bytes(clips["wav"].read_bytes())
    out = media.compress_audio(src)
    assert out.name == "tone Compressed.m4a" and acodec(out) == "aac"   # lossless -> AAC
    assert out.stat().st_size < src.stat().st_size
    mp3 = media.run(["-i", "file:" + str(src), "-c:a", "libmp3lame", "-b:a", "320k"], tmp_path / "hi.mp3", "mp3")
    small = media.compress_audio(mp3)
    assert small.suffix == ".mp3" and acodec(small) == "mp3"
    assert int(streams(small, "audio")[0]["bit_rate"]) <= 136_000
    assert media.compress(src).suffix == ".m4a", "Compress on an audio file compresses the audio"


@needs_ffmpeg
def test_normalize(clips, tmp_path):
    src = tmp_path / "tone.wav"
    src.write_bytes(clips["wav"].read_bytes())
    out = media.normalize(src)
    a = streams(out, "audio")[0]
    assert out.name == "tone Normalized.wav" and a["codec_name"] == "pcm_s16le"
    assert a["sample_rate"] == "44100", "loudnorm's 192 kHz is converted back"
    assert abs(duration(out) - 1.0) < DUR_TOL


@needs_ffmpeg
def test_strip_metadata(clips, tmp_path):
    src = tmp_path / "tagged.m4a"
    src.write_bytes(clips["tagged"].read_bytes())
    assert ffprobe(src)["format"]["tags"]["title"] == "Secret Title"
    out = media.strip_metadata(src)
    tags = {k.lower() for k in ffprobe(out)["format"].get("tags", {})}
    assert out.name == "tagged Clean.m4a" and not tags & {"title", "artist", "encoder"}
    assert acodec(out) == "aac"


# ---------------------------------------------------------------- encoders without x264
PLATFORM_H264 = ["h264_videotoolbox", "h264_mf", "libopenh264"]


@needs_ffmpeg
@pytest.mark.parametrize("encoder", PLATFORM_H264)
def test_h264_without_x264(clip, monkeypatch, encoder):
    """LGPL builds have no libx264: each platform encoder must give real H.264 when it works here."""
    if not media._usable_video_encoder(encoder):
        pytest.skip(f"{encoder} isn't usable with this ffmpeg")
    real = media.has_encoder
    hidden = {"libx264"} | set(PLATFORM_H264[:PLATFORM_H264.index(encoder)])
    monkeypatch.setattr(media, "has_encoder", lambda n: n not in hidden and real(n))
    assert media._h264(media.probe(clip), media.CRF_COMPRESS)[1] == encoder
    for out in (media.compress(clip), media.rotate(clip)):
        assert vcodec(out) == "h264" and acodec(out) == "aac"
        assert abs(duration(out) - 1.0) < DUR_TOL


@needs_ffmpeg
def test_no_h264_encoder_at_all(clip, monkeypatch):
    """Last resort is MPEG-4 Part 2, which every ffmpeg has and MP4/MOV/MKV accept."""
    monkeypatch.setattr(media, "has_encoder", lambda n: n in ("mpeg4", "aac"))
    assert vcodec(media.compress(clip)) == "mpeg4"
    assert vcodec(media.rotate(clip)) == "mpeg4"
    assert vcodec(convert.convert(clip, "video", "MKV")[0]) == "mpeg4"   # copy needs no encoder
    with pytest.raises(UserError, match="WebM"):
        convert.convert(clip, "video", "WEBM")
    with pytest.raises(UserError, match="MP3"):
        convert.convert(clip, "video", "MP3")


def test_encoder_list_parsing(monkeypatch):
    sample = (" V..... = Video\n ------\n V....D libx264              libx264 H.264\n"
              " A....D aac                  AAC (Advanced Audio Coding)\n")

    class R:
        stdout = sample
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: R())
    media._encoders.cache_clear()
    try:
        assert media._encoders("/fake/ffmpeg") == {"libx264", "aac"}
    finally:
        media._encoders.cache_clear()


# ---------------------------------------------------------------- progress and cancelling
@needs_ffmpeg
def test_progress_reported(clip):
    seen = []
    with media.reporting(seen.append):
        convert.convert(clip, "video", "WEBM" if media.has_encoder("libvpx-vp9") else "AVI")
    assert seen and seen[-1] == 1.0
    assert seen == sorted(seen) and all(0.0 <= f <= 1.0 for f in seen)


@needs_ffmpeg
def test_cancel(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()
    out = tmp_path / "long.wav"
    with pytest.raises(media.Cancelled):
        # -re reads the input in real time, so 30 s of tone takes 30 s unless cancelled
        media.run(["-re", "-f", "lavfi", "-i", "sine=duration=30"], out, "wav", 30, cancel=cancel)
    assert only_files(tmp_path) == set(), "the partial file is removed"


@needs_ffmpeg
def test_failure_message_is_plain(tmp_path, capsys):
    with pytest.raises(UserError) as e:
        media.run(["-f", "lavfi", "-i", "sine=duration=1", "-c:a", "no_such_codec"], tmp_path / "x.wav", "wav")
    assert "ffmpeg" in str(e.value) and "no_such_codec" not in str(e.value)
    assert "no_such_codec" in capsys.readouterr().err, "the detail goes to the log"
    assert only_files(tmp_path) == set()


def test_fmt_duration():
    assert media.fmt_duration(0.4) == "0:00"
    assert media.fmt_duration(42) == "0:42"
    assert media.fmt_duration(187) == "3:07"
    assert media.fmt_duration(3723) == "1:02:03"


# ---------------------------------------------------------------- summaries
@needs_ffmpeg
def test_summaries(clip):
    mkv = convert.convert(clip, "video", "MKV")
    r = report.summarize("convert:MKV", [clip], mkv)
    assert r.title == "MP4 → MKV" and "160×120" in r.detail and "0:01" in r.detail
    mp3 = convert.convert(clip, "video", "MP3")
    r = report.summarize("convert:MP3", [clip], mp3)
    assert "0:01" in r.detail and "×" not in r.detail
    r = report.summarize("tool:Rotate", [clip], [media.rotate(clip)])
    assert r.title == "Rotated 90°" and "120×160" in r.detail and "None" not in r.detail
    r = report.summarize("tool:Frame", [clip], [media.frame_grab(clip)])
    assert r.title == "Frame saved" and r.thumb is not None
    r = report.summarize("tool:Compress", [clip], [media.compress(clip)])
    assert "→" in r.detail
    r = report.summarize("tool:Crop", [clip], [media.crop(clip, (0, 0, 80, 60))])
    assert "160×120 → 80×60" in r.detail


# ---------------------------------------------------------------- packaging scripts
def load_packaging(name: str):
    path = Path(__file__).resolve().parent.parent / "packaging" / "ffmpeg" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ffmpeg_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fetch():
    return load_packaging("fetch")


def test_fetch_table_is_pinned():
    fetch = load_fetch()
    assert set(fetch.BUILDS) == {"windows-x64", "linux-x64", "macos-arm64", "macos-x64"}
    for key, build in fetch.BUILDS.items():
        assert build["licence"] == "LGPL-3.0-or-later", "nothing GPL ships"
        for name in fetch.LICENCE_FILES[build["licence"]]:
            assert (fetch.HERE / name).is_file()
        if "builder" in build:
            assert key.startswith("macos") and (fetch.HERE / build["builder"]).is_file()
            continue
        names = set()
        for a in build["archives"]:
            assert a["url"].startswith("https://") and "latest" not in a["url"]
            assert len(a["sha256"]) == 64 and int(a["sha256"], 16) >= 0
            names |= set(a["members"].values())
        exe = ".exe" if key.startswith("windows") else ""
        assert {f"ffmpeg{exe}", f"ffprobe{exe}"} <= names


def test_fetch_verify_and_extract(tmp_path):
    fetch = load_fetch()
    with pytest.raises(SystemExit, match="mismatch"):
        fetch.verify(b"tampered", "0" * 64, "https://example.invalid/x.zip")
    fetch.verify(b"ok", fetch.sha256(b"ok"), "u")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pkg/bin/ffmpeg", b"bin")
        z.writestr("pkg/LICENSE.txt", b"lic")
    fetch.extract(buf.getvalue(), "x.zip", {"pkg/bin/ffmpeg": "ffmpeg", "pkg/LICENSE.txt": "L.txt"}, tmp_path)
    assert (tmp_path / "ffmpeg").read_bytes() == b"bin" and (tmp_path / "L.txt").read_bytes() == b"lic"
    if sys.platform != "win32":
        assert (tmp_path / "ffmpeg").stat().st_mode & 0o111

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as t:
        info = tarfile.TarInfo("pkg/bin/ffprobe")
        info.size = 3
        t.addfile(info, io.BytesIO(b"prb"))
    fetch.extract(buf.getvalue(), "x.tar.xz", {"pkg/bin/ffprobe": "ffprobe"}, tmp_path)
    assert (tmp_path / "ffprobe").read_bytes() == b"prb"
    with pytest.raises((SystemExit, KeyError)):
        fetch.extract(buf.getvalue(), "x.tar.xz", {"pkg/bin/missing": "m"}, tmp_path)


def test_build_mac_sources_are_pinned():
    build_mac = load_packaging("build_mac")
    fetch = load_fetch()
    assert build_mac.SOURCES["ffmpeg"]["url"] == fetch.SOURCE, "same FFmpeg release as the other platforms"
    for name, src in build_mac.SOURCES.items():
        assert src["url"].startswith("https://") and "latest" not in src["url"], name
        assert len(src["sha256"]) == 64 and int(src["sha256"], 16) >= 0, name
        assert src["url"].endswith((".tar.gz", ".tar.xz")), name
    assert set(build_mac.SOURCES) - {"ffmpeg"} == {"lame", "libogg", "libvorbis", "opus", "libvpx"}
    licence = (fetch.HERE / "LICENSE.md").read_text(encoding="utf-8")
    for src in build_mac.SOURCES.values():
        assert src["url"] in licence, "LICENSE.md must list every source the Mac build uses"
    for flag in build_mac.FFMPEG_FLAGS:
        assert flag in licence, f"LICENSE.md must show the configure flags ({flag} is missing)"


def test_build_mac_configure_is_lgpl_only():
    build_mac = load_packaging("build_mac")
    flags = build_mac.FFMPEG_FLAGS
    assert not set(flags) & {"--enable-gpl", "--enable-nonfree", "--enable-libx264", "--enable-libx265"}
    assert "--enable-version3" in flags and "--disable-autodetect" in flags
    assert {"--enable-videotoolbox", "--enable-libmp3lame", "--enable-libopus", "--enable-libvpx"} <= set(flags)
    assert build_mac.configure_line().startswith("./configure --enable-version3")
    # every encoder media.py can ask for when no x264 is around must be checked after the build
    assert {"h264_videotoolbox", "libvpx-vp9", "libopus", "libmp3lame", "libvorbis", "aac"} <= \
        build_mac.REQUIRED_ENCODERS


def test_build_mac_linkage_check():
    build_mac = load_packaging("build_mac")
    clean = ("/x/ffmpeg:\n"
             "\t/usr/lib/libz.1.dylib (compatibility version 1.2.12, current version 1.2.12)\n"
             "\t/System/Library/Frameworks/VideoToolbox.framework/Versions/A/VideoToolbox (x)\n")
    assert build_mac.foreign_libs(clean) == []
    dirty = clean + "\t/opt/homebrew/opt/libvpx/lib/libvpx.11.dylib (compatibility version 11.0.0)\n" \
                    "\t@rpath/libopus.0.dylib (compatibility version 11.0.0)\n"
    assert build_mac.foreign_libs(dirty) == ["/opt/homebrew/opt/libvpx/lib/libvpx.11.dylib",
                                             "@rpath/libopus.0.dylib"]


def test_build_mac_encoder_listing_and_env(tmp_path, monkeypatch):
    build_mac = load_packaging("build_mac")
    listing = " V..... = Video\n ------\n V....D libvpx-vp9  libvpx VP9\n A....D aac  AAC\n"
    assert build_mac.encoder_names(listing) == {"libvpx-vp9", "aac"}
    monkeypatch.setenv("PKG_CONFIG_PATH", "/opt/homebrew/lib/pkgconfig")
    monkeypatch.setenv("CPATH", "/opt/homebrew/include")
    env = build_mac.build_env(tmp_path)
    assert "PKG_CONFIG_PATH" not in env and "CPATH" not in env, "Homebrew must not leak into the build"
    assert env["PKG_CONFIG_LIBDIR"] == str(tmp_path / "lib" / "pkgconfig")
    assert env["MACOSX_DEPLOYMENT_TARGET"] == build_mac.MIN_MACOS


def test_build_mac_stamp_tracks_arch(monkeypatch):
    build_mac = load_packaging("build_mac")
    monkeypatch.setattr(build_mac.platform, "machine", lambda: "arm64")
    arm = build_mac.stamp()
    monkeypatch.setattr(build_mac.platform, "machine", lambda: "x86_64")
    assert build_mac.stamp() != arm and len(arm) == 64


def test_fetch_refuses_to_cross_build_mac(tmp_path, monkeypatch):
    fetch = load_fetch()
    monkeypatch.setattr(fetch, "current_platform", lambda: "linux-x64")
    with pytest.raises(SystemExit, match="only be built on macos-arm64"):
        fetch.fetch("macos-arm64", tmp_path / "out")
    assert not (tmp_path / "out").exists(), "refuses before touching the destination"


@needs_ffmpeg
def test_job_progress_and_cancel(tmp_path):
    """The app's background Job wires ffmpeg progress and cancelling through media.reporting."""
    from dragshift import app
    out = tmp_path / "o.wav"
    job = app.Job("convert:WAV", [str(out)],
                  lambda: [media.run(["-re", "-f", "lavfi", "-i", "sine=duration=30"], out, "wav", 30)])
    got = {"progress": []}
    job.sig.failed.connect(lambda m: got.setdefault("failed", m))
    job.sig.progress.connect(got["progress"].append)
    threading.Timer(1.5, job.cancel.set).start()
    job.run()
    # progress is emitted from ffmpeg's reader thread, so it arrives through the event loop
    from PySide6.QtWidgets import QApplication
    (QApplication.instance() or QApplication([])).processEvents()
    assert got.get("failed") == "Cancelled."
    assert got["progress"] and 0 < max(got["progress"]) < 0.5
    assert only_files(tmp_path) == set()
