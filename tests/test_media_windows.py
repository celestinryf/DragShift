"""Video and audio tool windows (dragshift/media_windows.py): the time, speed and crop maths,
trim validation, and windows that build, paint and save for a generated clip and tone.
Window tests skip cleanly when ffmpeg isn't installed; outputs are checked with ffprobe."""
import gc
import json
import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QValidator  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dragshift import media, report  # noqa: E402
from dragshift import media_windows as mw  # noqa: E402
from dragshift.tools import ToolWindow, crop_box  # noqa: E402
from dragshift.util import UserError  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not media.available(), reason="ffmpeg/ffprobe not installed")
DUR_TOL = 0.15


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


def settle():
    """Finish preview work, then destroy closed windows and collect garbage here on the UI thread.
    Tests have no event loop, so closed windows (from this file or earlier ones) would otherwise
    wait for Python's garbage collector, which may next run on a preview worker thread."""
    mw.drain()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    gc.collect()


@pytest.fixture(autouse=True)
def tidy(qapp):
    settle()
    yield
    for w in QApplication.topLevelWidgets():
        if isinstance(w, ToolWindow):
            w.close()
    settle()


def ffprobe(path: Path) -> dict:
    out = subprocess.run([media.find_tool("ffprobe"), "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", "file:" + str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def duration(path: Path) -> float:
    return float(ffprobe(path)["format"]["duration"])


def vsize(path: Path) -> tuple[int, int]:
    v = [s for s in ffprobe(path)["streams"] if s["codec_type"] == "video"][0]
    return v["width"], v["height"]


def codecs(path: Path) -> dict:
    return {s["codec_type"]: s["codec_name"] for s in ffprobe(path)["streams"]}


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not media.available():
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("media_windows")
    return {"video": media.make_test_clip(d / "clip.mp4", seconds=2.0),
            "audio": media.make_test_clip(d / "tone.wav", seconds=2.0, video=False, acodec="pcm_s16le",
                                          muxer="wav")}


@pytest.fixture
def clip(clips, tmp_path):
    """A private copy, so outputs from one test don't change another's names."""
    def copy(kind):
        src = tmp_path / clips[kind].name
        src.write_bytes(clips[kind].read_bytes())
        return src
    return copy


def saved(window) -> list[Path]:
    """Run the window's primary action without the app (synchronously) and return the outputs."""
    got = []
    window.notify = lambda action, srcs, outs, extra="": got.extend(Path(o) for o in outs)
    window.primary_btn.click()
    return got


# ---------------------------------------------------------------- time text
@pytest.mark.parametrize("secs, text", [
    (0, "0:00.0"), (5.24, "0:05.2"), (5.26, "0:05.3"), (59.96, "1:00.0"), (61.5, "1:01.5"),
    (3600, "1:00:00.0"), (3723.46, "1:02:03.5"), (36000, "10:00:00.0"), (-3, "0:00.0"),
    (float("nan"), "0:00.0"), (float("inf"), "0:00.0"),
])
def test_fmt_time(secs, text):
    assert mw.fmt_time(secs) == text


@pytest.mark.parametrize("text, secs", [
    ("0", 0.0), ("5", 5.0), ("5.5", 5.5), ("5,5", 5.5), (".5", 0.5), ("90", 90.0), ("1:05", 65.0),
    ("1:05.3", 65.3), ("75:00", 4500.0), ("1:02:03.5", 3723.5), ("  0:00.0 ", 0.0), ("10:00:00.0", 36000.0),
])
def test_parse_time(text, secs):
    assert mw.parse_time(text) == pytest.approx(secs)


@pytest.mark.parametrize("text", ["", " ", "abc", "1:", ":5", "1:60", "1:00:60", "1:60:00", "-5", "1e3",
                                  "inf", "nan", "1:2:3:4", "5..5", "1:5a"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        mw.parse_time(text)


def test_parse_round_trips_format():
    for secs in (0, 0.1, 9.9, 59.9, 60, 3599.9, 3600, 86399.9):
        assert mw.parse_time(mw.fmt_time(secs)) == pytest.approx(secs)


def test_time_field():
    f = mw.TimeField(120.0)
    f.setValue(65.3)
    assert f.text() == "1:05.3"
    assert f.validate("1:10", 0)[0] == QValidator.Acceptable
    assert f.validate("1:", 0)[0] == QValidator.Intermediate     # still typing
    assert f.validate("3:00", 0)[0] == QValidator.Intermediate   # past the end of the clip
    assert f.valueFromText("rubbish") == pytest.approx(65.3)    # keeps the old value
    f.stepBy(1)
    assert f.value() == pytest.approx(65.4)


# ---------------------------------------------------------------- speed
def test_speed_slider_mapping():
    assert mw.slider_to_speed(0) == 1.0
    assert mw.slider_to_speed(-200) == 0.25 and mw.slider_to_speed(200) == 4.0
    assert mw.slider_to_speed(-999) == 0.25 and mw.slider_to_speed(999) == 4.0   # clamped
    for f in mw.SPEEDS:   # every preset survives the round trip through the slider
        assert mw.slider_to_speed(mw.speed_to_slider(f)) == f
    assert mw.slider_to_speed(37) == 1.3   # snaps to 0.05 steps


def test_speed_duration_and_label():
    assert mw.sped_duration(6.0, 2.0) == 3.0
    assert mw.sped_duration(6.0, 0.25) == 24.0
    assert mw.sped_duration(0.0, 4.0) == 0.0
    assert mw.speed_label(0.25) == "0.25×" and mw.speed_label(2.0) == "2×"


# ---------------------------------------------------------------- crop maths
def test_preview_size():
    assert mw.preview_size((1920, 1080)) == (960, 540)
    assert mw.preview_size((1080, 1920)) == (540, 960)
    assert mw.preview_size((320, 240)) == (320, 240)   # never upscaled
    assert mw.preview_size((5000, 1)) == (960, 2)      # never zero


def test_crop_box_snaps_and_keeps_ratio():
    # 1000x500 preview of a 2000x1000 image; a selection 1 px off the edges snaps to them
    assert crop_box(QRectF(1, 1, 998, 498), 2.0, (2000, 1000), None) == (0, 0, 2000, 1000)
    x, y, w, h = crop_box(QRectF(100, 50, 400.3, 300), 2.0, (2000, 1000), 4 / 3)
    assert (x, y) == (200, 100) and w / h == pytest.approx(4 / 3, abs=0.003)


def test_video_crop_rect_is_even_and_inside():
    x, y, w, h = mw.video_crop_rect(QRectF(5.3, 7.7, 100.6, 61.2), 2.0, (1920, 1080), None)
    assert all(v % 2 == 0 for v in (x, y, w, h))
    assert (x, y, w, h) == (10, 14, 200, 122)
    # a full selection of an odd-sized video loses the last column/row
    assert mw.video_crop_rect(QRectF(0, 0, 161, 121), 1.0, (161, 121), None) == (0, 0, 160, 120)


def test_video_crop_rect_with_rotation():
    """A phone clip stored 1920x1080 with 90° rotation is cropped in its upright 1080x1920
    frame, which is what the preview shows and what media.crop takes."""
    info = media.Info(10.0, "mov", video=[media.Stream(0, "video", "h264", 1920, 1080, 30, rotation=90)])
    display = info.size
    assert display == (1080, 1920)
    pv = mw.preview_size(display)
    k = display[0] / pv[0]
    assert pv == (540, 960) and k == 2.0
    # the bottom half of the preview
    assert mw.video_crop_rect(QRectF(0, 480, 540, 480), k, display, None) == (0, 960, 1080, 960)
    # 1:1 from the middle stays square and even
    x, y, w, h = mw.video_crop_rect(QRectF(0, 210, 540, 540), k, display, 1.0)
    assert w == h == 1080 and y % 2 == 0 and y + h <= 1920


# ---------------------------------------------------------------- trim validation
def test_trim_range():
    assert media.trim_range(1.0, 2.0, 5.0) == (1.0, 2.0)
    assert media.trim_range(1.0, 99.0, 5.0) == (1.0, 5.0)    # end clamped to the clip
    assert media.trim_range(1.0, 99.0, 0.0) == (1.0, 99.0)   # unknown duration: trust the user
    for start, end in ((2.0, 2.0), (2.0, 1.0), (-1.0, 1.0), (1.0, 1.01)):
        with pytest.raises(UserError, match="before the end"):
            media.trim_range(start, end, 5.0)
    with pytest.raises(UserError, match="past the end"):
        media.trim_range(5.0, 6.0, 5.0)


# ---------------------------------------------------------------- the timeline
def test_timeline_bar_handles_and_keys():
    bar = mw.TimelineBar(10.0, fps=25, trim=True)
    bar.resize(524, 56)   # track is 500 px wide: 50 px per second
    got = {"trim": None, "scrub": []}
    bar.trimmed.connect(lambda lo, hi: got.update(trim=(lo, hi)))
    bar.scrubbed.connect(got["scrub"].append)
    assert bar.time_at(bar.x_of(4.0)) == pytest.approx(4.0)
    assert bar._hit(bar.x_of(0) - 6) == "lo" and bar._hit(bar.x_of(10) + 6) == "hi"
    assert bar._hit(bar.x_of(5)) == "scrub"
    bar._move_to("lo", 2.0)
    bar._move_to("hi", 1.0)   # can't cross the start
    assert got["trim"] == (2.0, pytest.approx(2.0 + bar.min_len))
    assert bar.t == pytest.approx(2.0 + bar.min_len)   # the play head follows the handle
    bar.set_range(2.0, 8.0)
    bar._move_to("scrub", 9.5)
    assert bar.t == 8.0   # play head stays inside the selection
    bar.setFocus()
    from PySide6.QtTest import QTest
    QTest.keyClick(bar, Qt.Key_Left)
    assert bar.t == pytest.approx(8.0 - 0.04)   # one frame at 25 fps
    QTest.keyClick(bar, Qt.Key_Home)
    assert bar.t == 2.0 and got["scrub"][-1] == 2.0
    assert not bar.grab().isNull()


# ---------------------------------------------------------------- windows (need ffmpeg)
def wait(w):
    w.loader.wait()
    QApplication.processEvents()


@needs_ffmpeg
def test_windows_build_and_paint(clip):
    video, audio = clip("video"), clip("audio")
    n = lambda *a, **k: None  # noqa: E731
    wins = [mw.TrimWindow(str(video), n), mw.TrimWindow(str(audio), n), mw.VideoCropWindow(str(video), n),
            mw.FrameWindow(str(video), n)]
    for w in wins:
        w.resize(1000, 680)
        wait(w)
        assert not w.grab().isNull(), type(w).__name__
        assert w.primary_btn is not None and w.primary_btn.isDefault()
        assert not w.loader.busy()
    assert len(wins[0].bar.strip) == mw.STRIP_THUMBS and all(not i.isNull() for i in wins[0].bar.strip)
    assert wins[1].bar.wave is not None and not wins[1].bar.wave.isNull() and wins[1].view is None
    assert wins[3].view.image.size().toTuple() == (160, 120)   # the real frame replaced the blank
    for w in wins:
        w.close()
    for path in (video, audio):
        s = mw.SpeedWindow(str(path), n)
        assert not s.grab().isNull()
        s.close()
    assert sorted(p.name for p in video.parent.iterdir()) == ["clip.mp4", "tone.wav"]   # previews write nothing


@needs_ffmpeg
def test_frame_window_needs_video(clip):
    with pytest.raises(UserError, match="no video"):
        mw.FrameWindow(str(clip("audio")), lambda *a: None)
    with pytest.raises(UserError, match="no video"):
        mw.VideoCropWindow(str(clip("audio")), lambda *a: None)


@needs_ffmpeg
def test_scrubbing_uses_the_cache(clip):
    w = mw.FrameWindow(str(clip("video")), lambda *a: None)
    wait(w)
    w.go_to(0.5)
    w._frame_timer.stop()   # fire the debounce now instead of waiting
    w._request_frame()
    wait(w)
    key = w._frame_key(0.5)
    assert mw.FRAMES.get(key) is not None
    requests = []
    w.loader.request = lambda *a: requests.append(a)
    w.go_to(0.5)        # cached: shown straight away, no ffmpeg
    assert not w._frame_timer.isActive() and not requests
    assert w.time.value() == pytest.approx(0.5)
    w.close()


@needs_ffmpeg
def test_trim_window_validates_and_trims(clip):
    w = mw.TrimWindow(str(clip("video")), lambda *a: None)
    assert w.end.value() == pytest.approx(2.0) and w.primary_btn.isEnabled()
    w.start.setValue(1.5)
    w.end.setValue(1.5)
    assert not w.primary_btn.isEnabled() and not w.warn.isHidden()
    w.start.setValue(0.5)
    w.end.setValue(1.5)
    assert w.primary_btn.isEnabled() and w.warn.isHidden()
    assert w.length.text() == "0:01.0"
    assert (w.bar.lo, w.bar.hi) == (0.5, 1.5)
    out, = saved(w)
    assert out.name == "clip Trimmed.mp4" and abs(duration(out) - 1.0) < DUR_TOL


@needs_ffmpeg
def test_trim_window_set_start_end_and_fast(clip):
    src = clip("video")
    w = mw.TrimWindow(str(src), lambda *a: None)
    w.go_to(0.4)
    w.set_start()
    w.go_to(1.6)
    w.set_end()
    assert w.selection() == (pytest.approx(0.4), pytest.approx(1.6))
    w.mode.button(1).click()
    out, = saved(w)
    # stream copy: same codecs, length within a keyframe interval of the request
    assert codecs(out) == codecs(src)
    assert abs(duration(out) - 1.2) < 0.6


@needs_ffmpeg
def test_trim_audio_window(clip):
    w = mw.TrimWindow(str(clip("audio")), lambda *a: None)
    w.start.setValue(0.25)
    w.end.setValue(1.0)
    out, = saved(w)
    assert out.suffix == ".wav" and abs(duration(out) - 0.75) < 0.05


@needs_ffmpeg
def test_crop_window_applies_even_size(clip):
    w = mw.VideoCropWindow(str(clip("video")), lambda *a: None)
    assert w.k == 1.0   # 160x120 is smaller than the preview size
    w.view.sel = QRectF(10.4, 20.6, 81.3, 61.1)
    assert w.crop_rect() == (10, 20, 80, 60)
    w.ratio_box.setCurrentIndex(2)   # 1:1
    x, y, cw, ch = w.crop_rect()
    assert cw == ch == 120
    out, = saved(w)
    assert out.name == "clip Cropped.mp4" and vsize(out) == (120, 120)


@needs_ffmpeg
def test_crop_window_rotated_clip(clip, tmp_path):
    """A clip with 90° rotation metadata is shown upright and cropped in upright pixels."""
    rotated = tmp_path / "phone.mp4"
    r = subprocess.run([media.find_tool("ffmpeg"), "-v", "error", "-display_rotation", "90", "-i",
                        "file:" + str(clip("video")), "-c", "copy", "file:" + str(rotated)])
    if r.returncode != 0 or media.probe(rotated).video[0].rotation % 180 == 0:   # -display_rotation is ffmpeg 6+
        pytest.skip("this ffmpeg can't write rotation metadata")
    w = mw.VideoCropWindow(str(rotated), lambda *a: None)
    wait(w)
    assert w.view.image.size().toTuple() == (120, 160)   # upright preview
    w.view.sel = QRectF(0, 0, 120, 80)                   # top half
    assert w.crop_rect() == (0, 0, 120, 80)
    out, = saved(w)
    assert vsize(out) == (120, 80)


@needs_ffmpeg
def test_speed_window(clip):
    w = mw.SpeedWindow(str(clip("video")), lambda *a: None)
    assert w.factor() == 2.0 and w.preset.currentText() == "2×"
    assert w.result.text() == "0:02.0  →  0:01.0"
    w.slider.setValue(0)
    assert not w.primary_btn.isEnabled()    # 1x would change nothing
    w.slider.setValue(37)
    assert w.preset.currentText() == "Custom"
    w.preset.setCurrentIndex(mw.SPEEDS.index(0.5))
    assert w.factor() == 0.5 and w.primary_btn.isEnabled()
    out, = saved(w)
    assert out.name == "clip 0.5x.mp4" and abs(duration(out) - 4.0) < DUR_TOL * 2


@needs_ffmpeg
def test_speed_window_pitch(clip):
    src = clip("audio")
    w = mw.SpeedWindow(str(src), lambda *a: None)
    w.pitch.setChecked(False)
    assert "tape" in w.pitch_note.text()
    out, = saved(w)
    info = media.probe(out)
    assert abs(info.duration - 1.0) < 0.05 and info.audio[0].sample_rate == 44100


def test_pitch_shift_filters():
    assert media._pitch_shift(2.0, 44100) == ["asetrate=88200", "aresample=44100"]
    assert media._pitch_shift(0.5, 48000) == ["asetrate=24000", "aresample=48000"]


@needs_ffmpeg
def test_frame_window_saves_chosen_frame(clip):
    w = mw.FrameWindow(str(clip("video")), lambda *a: None)
    assert w.bar.t == pytest.approx(1.0)   # opens on the middle
    w.time.setValue(0.25)
    assert w.bar.t == pytest.approx(0.25)
    out, = saved(w)
    assert out.name == "clip Frame.png"
    from PIL import Image
    with Image.open(out) as im:
        assert im.size == (160, 120)


@needs_ffmpeg
def test_previews(clips):
    v = media.probe(clips["video"]).video[0]
    png = media.preview_frame(clips["video"], 1.0, (80, 60), v.index)
    assert png[:4] == b"\x89PNG"
    assert media.preview_frame(clips["video"], 1.0, (80, 60), v.index, exact=False)[:4] == b"\x89PNG"
    assert media.waveform(clips["audio"], (200, 40))[:4] == b"\x89PNG"
    with pytest.raises(UserError):
        media.waveform(clips["video"], (200, 40), index=v.index)   # a video stream has no waveform


@needs_ffmpeg
def test_media_summaries_include_extra(clip):
    src = clip("video")
    out = media.speed(src, 2.0)
    r = report.summarize("tool:Speed", [src], [out], "2×")
    assert r.title == "Speed changed" and "2×" in r.detail


def test_loader_runs_off_the_ui_thread_and_keeps_only_the_newest():
    import threading
    from PySide6.QtCore import QObject
    owner = QObject()
    loader = mw.Loader(owner)
    gate, got, threads = threading.Event(), [], []
    loader.loaded.connect(lambda kind, key, result: got.append((kind, key, result)))

    def job(n):
        def run():
            threads.append(threading.current_thread() is threading.main_thread())
            gate.wait(5)
            return n
        return run
    loader.request("frame", 1, job(1))
    loader.request("frame", 2, job(2))   # replaced by 3 before it starts
    loader.request("frame", 3, job(3))
    loader.request("other", "x", lambda: 1 / 0)   # a failing preview delivers None
    gate.set()
    loader.wait()
    assert [key for kind, key, _ in got if kind == "frame"] == [1, 3]
    assert ("other", "x", None) in got
    assert threads == [False, False]   # 2 never ran


# ---------------------------------------------------------------- cancelling the waveform
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe (not on Windows)")


@pytest.fixture
def stuck(tmp_path, monkeypatch):
    """A named pipe nobody writes to, so ffmpeg blocks opening it until killed, plus the
    processes started on it (to check they are gone afterwards)."""
    fifo = tmp_path / "stuck.wav"
    os.mkfifo(fifo)
    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(cmd, *a, **k):
        proc = real_popen(cmd, *a, **k)
        if any(str(fifo) in str(arg) for arg in cmd):   # not the window's ffprobe of the real clip
            procs.append(proc)
        return proc
    monkeypatch.setattr(media.subprocess, "Popen", popen)
    yield fifo, procs
    for p in procs:   # never leave an ffmpeg behind, even when a test fails
        if p.poll() is None:
            p.kill()
            p.wait()


def wait_for(cond, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


@needs_ffmpeg
@needs_fifo
def test_cancelling_the_waveform_stops_ffmpeg_promptly(stuck):
    import threading
    import time
    fifo, procs = stuck
    cancel, raised = threading.Event(), []

    def run():
        try:
            media.waveform(fifo, (200, 40), cancel=cancel)
        except Exception as e:  # noqa: BLE001  (checked below)
            raised.append(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert wait_for(lambda: procs), "ffmpeg never started"
    time.sleep(0.3)
    assert procs[0].poll() is None and not raised   # really stuck until cancelled
    start = time.monotonic()
    cancel.set()
    t.join(3)
    assert not t.is_alive() and time.monotonic() - start < 2
    assert len(raised) == 1 and isinstance(raised[0], media.Cancelled)
    assert procs[0].poll() is not None   # killed and reaped, not left running


@needs_ffmpeg
@needs_fifo
def test_a_stuck_preview_times_out_and_stops_ffmpeg(stuck):
    fifo, procs = stuck
    with pytest.raises(UserError) as e:
        media._pipe(["-i", media._file(fifo), "-f", "wav"], timeout=0.3)
    assert not isinstance(e.value, media.Cancelled)
    assert procs and procs[0].poll() is not None


@needs_ffmpeg
@needs_fifo
def test_closing_the_trim_window_cancels_its_waveform(clip, stuck, monkeypatch):
    import time
    fifo, procs = stuck
    real_waveform, seen = media.waveform, []

    def waveform(path, size, index=0, cancel=None):
        # the window's real call, on a file that never finishes loading
        seen.append(cancel)
        return real_waveform(fifo, size, index, cancel)
    monkeypatch.setattr(media, "waveform", waveform)
    w = mw.TrimWindow(str(clip("audio")), lambda *a: None)
    assert wait_for(lambda: procs), "the waveform job never started ffmpeg"
    assert procs[0].poll() is None
    start = time.monotonic()
    w.close()
    mw.drain(5)
    assert time.monotonic() - start < 2
    assert seen == [w.loader.stop] and w.loader.stop.is_set()
    assert procs[0].poll() is not None   # ffmpeg is gone
    assert w.bar.wave is None            # and the cancelled job delivered nothing
