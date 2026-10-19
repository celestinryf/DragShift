"""Workstream 7: SVG input (svg.py), QR reading (qr.py), PDF page reorder (reorder.py), compress by
percentage (compress.py, tools.CompressWindow, media), and how they and the archive/subtitle
formats are wired into the rings, the menu hints, SF Symbols and the report."""
import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dragshift import app, compress, convert, design, imaging, media, qr, report, svg, tools  # noqa: E402
from dragshift.app import DragShiftApp  # noqa: E402
from dragshift.convert import fitz  # noqa: E402
from dragshift.radial import Item  # noqa: E402
from dragshift.reorder import ReorderWindow, reorder_pdf  # noqa: E402
from dragshift.util import UserError, category, file_size  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def collect_windows():
    """Free each test's windows here, on the UI thread. Left to the garbage collector, their
    reference cycles (signal lambdas) can be collected on an ffmpeg reader thread in a later
    test, and Qt objects destroyed off their own thread crash."""
    yield
    import gc
    for w in QApplication.topLevelWidgets():
        if not isinstance(w, tools.ToolWindow):
            continue
        w.close()
        w.deleteLater()
    gc.collect()
    QApplication.processEvents()


def notify_log():
    calls = []
    return calls, lambda *a, **k: calls.append(a)


# ---------------------------------------------------------------- SVG
def svg_file(tmp_path: Path, name: str, attrs: str, body: str = "") -> Path:
    p = tmp_path / name
    p.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>', encoding="utf-8")
    return p


RED = '<rect width="100%" height="100%" fill="#ff0000"/>'


@pytest.mark.parametrize("w, h, expected", [
    (24, 12, (1024, 512)),        # tiny icons are scaled up
    (2000, 1000, (2000, 1000)),   # already big enough: as drawn
    (20000, 5000, (8192, 2048)),  # capped
    (1, 1, (1024, 1024)),
])
def test_raster_size(w, h, expected):
    assert svg.raster_size(w, h) == expected


def test_svg_category_and_targets():
    assert category("logo.svg") == category("LOGO.SVG") == "svg"
    t = convert.targets_for("svg", ".svg")
    assert t[:3] == ["PNG", "JPG", "WEBP"] and "PDF" in t and "SVG" not in t
    hints = {**app.FORMAT_HINTS, **app.CATEGORY_FORMAT_HINTS.get("svg", {})}
    assert all(hints.get(x) for x in t)


def test_svg_to_png_size_and_pixels(tmp_path):
    src = svg_file(tmp_path, "icon.svg", 'width="24" height="12" viewBox="0 0 24 12"',
                   '<rect width="12" height="12" fill="#ff0000"/><rect x="12" width="12" height="12" fill="#0000ff"/>')
    out = convert.convert(src, "svg", "PNG")[0]
    assert out.name == "icon.png"
    with Image.open(out) as im:
        assert im.size == (1024, 512) and im.mode == "RGBA"
        assert im.getpixel((100, 256))[:3] == (255, 0, 0)
        assert im.getpixel((900, 256))[:3] == (0, 0, 255)


def test_svg_with_only_a_viewbox_uses_it(tmp_path):
    src = svg_file(tmp_path, "vb.svg", 'viewBox="0 0 300 100"', RED)
    with Image.open(svg.convert(src, "JPG")[0]) as im:
        assert im.size == (1024, 341) and im.format == "JPEG"


def test_svg_transparency_is_kept_in_png_and_flattened_in_jpg(tmp_path):
    src = svg_file(tmp_path, "dot.svg", 'width="100" height="100"', '<circle cx="50" cy="50" r="10" fill="#000"/>')
    with Image.open(svg.convert(src, "PNG")[0]) as im:
        assert im.getpixel((5, 5))[3] == 0
    with Image.open(svg.convert(src, "JPG")[0]) as im:
        assert im.getpixel((5, 5)) == (255, 255, 255)


def test_svg_to_pdf_is_vector_at_its_own_size(tmp_path):
    src = svg_file(tmp_path, "doc.svg", 'width="400" height="200"',
                   '<rect width="400" height="200" fill="#eee"/><text x="20" y="100" font-size="40">Hello SVG</text>')
    out = svg.convert(src, "PDF")[0]
    with fitz.open(out) as d:
        assert d.page_count == 1
        assert d[0].rect.width == pytest.approx(300, abs=1) and d[0].rect.height == pytest.approx(150, abs=1)
        assert "Hello SVG" in d[0].get_text()          # text stays text


def test_damaged_or_sizeless_svg_is_a_user_error(tmp_path):
    bad = tmp_path / "bad.svg"
    bad.write_text("<svg><unclosed", encoding="utf-8")
    with pytest.raises(UserError, match="damaged"):
        svg.convert(bad, "PNG")
    assert not (tmp_path / "bad.png").exists()


SECRET_IMAGE = '<image width="100" height="100" {attr}="{href}"/>'


def test_svg_never_draws_local_files(tmp_path):
    """<image> (and filter feImage) pointing at a file is dropped: an SVG from the web can't pull
    other local files into the result, whether by absolute path, ../ or a symlink."""
    secret = tmp_path / "secret"
    secret.mkdir()
    Image.new("RGB", (10, 10), "red").save(secret / "private.png")
    folder = tmp_path / "dl"
    folder.mkdir()
    refs = [str(secret / "private.png"), "../secret/private.png", (secret / "private.png").as_uri()]
    if sys.platform != "win32":
        (folder / "link.png").symlink_to(secret / "private.png")
        refs.append("link.png")
    xlink = 'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100"'
    for i, ref in enumerate(refs):
        for attr in ("xlink:href", "href"):
            src = svg_file(folder, f"img{i}{attr[0]}.svg", xlink, SECRET_IMAGE.format(attr=attr, href=ref))
            with Image.open(svg.convert(src, "PNG")[0]) as im:
                assert im.convert("RGBA").getpixel((im.width // 2, im.height // 2))[3] == 0, (ref, attr)
        filt = (f'<filter id="f"><feImage xlink:href="{ref}"/></filter>'
                '<rect width="100" height="100" filter="url(#f)"/>')
        src = svg_file(folder, f"filter{i}.svg", xlink, filt)
        assert b"feImage" not in svg.safe_svg(src)
        with Image.open(svg.convert(src, "PNG")[0]) as im:
            assert im.convert("RGB").getpixel((im.width // 2, im.height // 2)) != (255, 0, 0), ref
    assert b"<image" not in svg.safe_svg(svg_file(folder, "x.svg", xlink, SECRET_IMAGE.format(attr="href", href=refs[0])))


def test_svg_keeps_embedded_pictures_internal_links_and_entities(tmp_path):
    import base64
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (0, 0, 255)).save(buf, "PNG")
    data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    src = tmp_path / "ok.svg"
    # an Illustrator-style internal entity for the namespace, a <use> of a local shape, a data: picture
    src.write_text('<?xml version="1.0"?>\n<!DOCTYPE svg [<!ENTITY ns_svg "http://www.w3.org/2000/svg">]>\n'
                   '<svg xmlns="&ns_svg;" xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
                   f'<image width="50" height="100" xlink:href="{data}"/>'
                   '<defs><rect id="r" width="50" height="100" fill="#00ff00"/></defs>'
                   '<use xlink:href="#r" x="50"/></svg>', encoding="utf-8")
    with Image.open(svg.convert(src, "PNG")[0]) as im:
        rgb = im.convert("RGB")
        assert rgb.getpixel((im.width // 4, im.height // 2)) == (0, 0, 255)
        assert rgb.getpixel((im.width * 3 // 4, im.height // 2)) == (0, 255, 0)


def test_svg_with_an_external_entity_is_refused(tmp_path):
    src = tmp_path / "xxe.svg"
    src.write_text(f'<?xml version="1.0"?>\n<!DOCTYPE svg [<!ENTITY x SYSTEM "{(tmp_path / "s.txt").as_uri()}">]>\n'
                   '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>&x;</text></svg>',
                   encoding="utf-8")
    (tmp_path / "s.txt").write_text("secret")
    with pytest.raises(UserError, match="damaged"):
        svg.convert(src, "PNG")


def test_svg_report_shows_dimensions(tmp_path):
    src = svg_file(tmp_path, "r.svg", 'width="24" height="24"', RED)
    outs = svg.convert(src, "PNG")
    r = report.summarize("convert:PNG", [src], outs)
    assert r.title == "SVG → PNG" and "1024×1024" in r.detail


# ---------------------------------------------------------------- QR
def qr_png(tmp_path: Path, *texts: str, name="code.png") -> Path:
    qrcode = pytest.importorskip("qrcode")
    tiles = [qrcode.make(t, border=4).get_image().convert("RGB") for t in texts]
    w = sum(t.width for t in tiles) + 40 * (len(tiles) + 1)
    h = max(t.height for t in tiles) + 80
    canvas = Image.new("RGB", (w, h), "white")
    x = 40
    for t in tiles:
        canvas.paste(t, (x, 40))
        x += t.width + 40
    p = tmp_path / name
    canvas.save(p)
    return p


def test_web_link_only_accepts_http_and_https():
    assert qr.web_link("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert qr.web_link("  http://x.org  ") == "http://x.org"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "mailto:a@b.c", "example.com", "https://",
                "https://exa mple.com", "ftp://x.org", "HTTPS//no-colon", "WIFI:S:home;T:WPA;P:pw;;"):
        assert qr.web_link(bad) is None, bad


def test_decode_reads_text_and_links(tmp_path):
    codes = qr.decode(qr_png(tmp_path, "https://example.com/über", "Plain text ✓"))
    assert [c.text for c in codes] == ["https://example.com/über", "Plain text ✓"]
    assert all(c.kind == "QR Code" for c in codes)
    assert codes[0].link and codes[1].link is None


def test_decode_lists_duplicates_once(tmp_path):
    assert [c.text for c in qr.decode(qr_png(tmp_path, "same", "same"))] == ["same"]


def test_decode_big_photo_is_scaled_first(tmp_path):
    src = qr_png(tmp_path, "big")
    with Image.open(src) as im:
        im.resize((im.width * 12, im.height * 12), Image.NEAREST).save(tmp_path / "huge.png")
    assert [c.text for c in qr.decode(tmp_path / "huge.png")] == ["big"]


def test_no_code_is_a_user_error(tmp_path):
    p = tmp_path / "blank.png"
    Image.new("RGB", (300, 300), "white").save(p)
    with pytest.raises(UserError, match="No QR code"):
        qr.QrWindow(p, lambda *a: None)


def test_qr_window_copies_and_never_opens_links_by_itself(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(qr.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    w = qr.QrWindow(qr_png(tmp_path, "https://example.com/x", "javascript:alert(1)"), lambda *a: None)
    assert opened == []                                     # nothing opens on its own
    assert len(w.copy_buttons) == 2 and len(w.link_buttons) == 1
    w.copy(w.codes[1], w.copy_buttons[1])
    assert QGuiApplication.clipboard().text() == "javascript:alert(1)"
    assert w.copy_buttons[1].text() == "Copied"
    w.link_buttons[0].click()
    assert opened == ["https://example.com/x"]
    w.open_link(w.codes[1])                                 # not a web link: refused
    assert opened == ["https://example.com/x"]
    assert not w.grab().isNull()
    w.close()


def test_qr_without_zxing_says_so(tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "zxingcpp":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(UserError, match="isn't available"):
        qr.decode(qr_png(tmp_path, "x"))


# ---------------------------------------------------------------- PDF page reorder
def numbered_pdf(path: Path, n: int = 4) -> Path:
    d = fitz.open()
    for i in range(n):
        page = d.new_page(width=300 + i * 10, height=400)
        page.insert_text((50, 100), f"Page {i + 1}", fontsize=24)
    d.save(path)
    d.close()
    return path


def page_texts(path: Path) -> list[str]:
    with fitz.open(path) as d:
        return [p.get_text().strip() for p in d]


def test_reorder_pdf_order_and_deletion(tmp_path):
    src = numbered_pdf(tmp_path / "Deck.pdf")
    out = reorder_pdf(src, [3, 0, 2])
    assert out.name == "Deck Reordered.pdf"
    assert page_texts(out) == ["Page 4", "Page 1", "Page 3"]
    assert page_texts(src) == ["Page 1", "Page 2", "Page 3", "Page 4"]     # original untouched
    with pytest.raises(UserError):
        reorder_pdf(src, [])
    with pytest.raises(ValueError):
        reorder_pdf(src, [0, 9])


def test_reorder_window_moves_deletes_and_saves(tmp_path):
    src = numbered_pdf(tmp_path / "Deck.pdf")
    calls, notify = notify_log()
    w = ReorderWindow(src, notify)
    assert w.order() == [0, 1, 2, 3]
    assert not w.primary_btn.isEnabled()                    # nothing changed yet
    w._render_some()
    assert not w.grid.item(0).icon().isNull()

    w.grid.item(3).setSelected(True)
    w.move_selected(-1)
    w.move_selected(-1)
    assert w.order() == [0, 3, 1, 2] and w.primary_btn.isEnabled()
    w.move_selected(-1)
    w.move_selected(-1)                                     # already first: no change
    assert w.order() == [3, 0, 1, 2]

    w.grid.clearSelection()
    w.grid.item(2).setSelected(True)                        # original page 2
    w.delete_selected()
    assert w.order() == [3, 0, 2]
    assert "1 deleted" in w.count.text()

    w.reverse()
    assert w.order() == [2, 0, 3]
    w.save()                                                # no app: saves synchronously
    action, srcs, outs, extra = calls[0]
    assert action == "tool:Reorder" and extra == "1 page deleted"
    assert page_texts(outs[0]) == ["Page 3", "Page 1", "Page 4"]
    r = report.summarize(action, srcs, outs, extra)
    assert r.title == "Pages reordered" and r.detail.startswith("3 pages · 1 page deleted")


def test_reorder_window_drag_move_updates_order(tmp_path):
    w = ReorderWindow(numbered_pdf(tmp_path / "D.pdf", 3), lambda *a: None)
    # what a drop does under the hood: the model moves the row
    assert w.grid.model().moveRow(QModelIndex(), 0, QModelIndex(), 3)
    assert w.order() == [1, 2, 0] and w.primary_btn.isEnabled()
    w.reset()
    assert w.order() == [0, 1, 2] and not w.primary_btn.isEnabled()
    w.close()


def test_reorder_window_keeps_the_last_page(tmp_path):
    w = ReorderWindow(numbered_pdf(tmp_path / "Two.pdf", 2), lambda *a: None)
    w.grid.selectAll()
    w.delete_selected()
    assert w.order() == [0, 1] and "at least one page" in w.count.text()
    w.close()


def test_reorder_window_refuses_protected_pdf(tmp_path):
    src = numbered_pdf(tmp_path / "P.pdf", 1)
    out = imaging.protect_pdf(src, "secret")
    with pytest.raises(UserError, match="password"):
        ReorderWindow(out, lambda *a: None)


# ---------------------------------------------------------------- compress by percentage
def noisy_jpeg(path: Path, size=(900, 700)) -> Path:
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, quality=95)
    return path


def test_image_compress_window_percentage_mode(tmp_path):
    src = noisy_jpeg(tmp_path / "noise.jpg")
    calls, notify = notify_log()
    w = tools.CompressWindow([str(src)], notify)
    w.mode.button(2).click()
    assert w.p_box.isVisibleTo(w) and not w.q_box.isVisibleTo(w)
    w.pct.setValue(30)
    w.estimate()
    assert "%" in w.saving.text() or "compressed" in w.saving.text()
    w.save()
    action, srcs, outs, extra = calls[0]
    assert extra == "aimed for 30%"
    assert file_size(outs[0]) <= file_size(src) * 0.30


def test_pdf_percent_hits_the_aim_when_pictures_allow(tmp_path):
    img = noisy_jpeg(tmp_path / "pic.jpg", (1600, 1600))
    src = tmp_path / "Scan.pdf"
    d = fitz.open()
    page = d.new_page(width=400, height=400)
    page.insert_image(page.rect, filename=str(img))
    d.save(src)
    d.close()
    out = compress.compress_pdf_percent(src, 40)
    assert out.name == "Scan Compressed.pdf"
    assert file_size(out) <= file_size(src) * 0.40
    assert fitz.open(out).page_count == 1


def test_pdf_percent_on_text_gives_the_smallest_it_can(tmp_path):
    src = numbered_pdf(tmp_path / "Text.pdf", 3)
    out = compress.compress_pdf_percent(src, 10)
    assert out.exists() and page_texts(out) == ["Page 1", "Page 2", "Page 3"]


def test_percent_window_modes_and_runs(tmp_path):
    src = numbered_pdf(tmp_path / "W.pdf", 2)
    calls, notify = notify_log()
    w = compress.pdf_window([str(src)], notify)
    assert w.percent() is None and w.blurb.isVisibleTo(w)
    w.save()
    assert calls[-1][0] == "tool:Compress" and calls[-1][3] == ""
    assert calls[-1][2][0].name == "W Compressed.pdf"

    w = compress.pdf_window([str(src), str(src)], notify)
    assert w.primary_btn.text() == "Compress 2 Files"
    w.mode.button(1).click()
    w.pct.setValue(25)
    assert w.percent() == 25 and "about" in w.estimate.text()
    seen = []
    w.by_percent = lambda p, pct: seen.append((Path(p).name, pct)) or Path(p)
    w.save()
    assert seen == [("W.pdf", 25), ("W.pdf", 25)] and calls[-1][3] == "aimed for 25%"


def test_media_windows_build_without_running_ffmpeg(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 1000)
    for make in (compress.video_window, compress.audio_window):
        w = make([str(f)], lambda *a: None)
        assert w.primary_btn.text() == "Compress" and not w.grab().isNull()
        w.close()


needs_ffmpeg = pytest.mark.skipif(not media.available(), reason="ffmpeg/ffprobe not installed")


@needs_ffmpeg
def test_video_compress_by_percent(tmp_path):
    src = media.make_test_clip(tmp_path / "busy.mp4", seconds=3, size="640x360", extra=["-q:v", "2"])
    out = media.compress(src, 30)
    ratio = file_size(out) / file_size(src)
    assert 0.1 < ratio < 0.45, ratio
    info = media.probe(out)
    assert info.size == (640, 360) and info.audio


@needs_ffmpeg
def test_video_percent_too_small_is_a_user_error(tmp_path):
    src = media.make_test_clip(tmp_path / "tiny.mp4", seconds=1)
    with pytest.raises(UserError, match="too small"):
        media.compress(src, 1)


@needs_ffmpeg
def test_audio_compress_by_percent(tmp_path):
    src = media.make_test_clip(tmp_path / "tone.m4a", seconds=2, video=False, acodec="aac", muxer="ipod",
                               extra=["-b:a", "192k"])
    out = media.compress_audio(src, 50)
    kbps = media.probe(out).audio[0].bit_rate / 1000
    assert out.suffix == ".m4a" and 60 <= kbps <= 120, kbps


# ---------------------------------------------------------------- wiring: rings, hints, symbols, routing
def rings(paths) -> dict:
    return DragShiftApp.build_rings(None, [str(p) for p in paths])


def labels(items) -> list[str]:
    return [i.label for i in items]


def test_new_tools_have_glyphs_hints_and_symbols():
    for name in ("Read QR", "Reorder", "Extract Files", "Zip"):
        assert name in app.GLYPHS and name in app.HINTS and name in design.SYMBOLS, name
    assert set(app.GLYPHS) <= set(design.SYMBOLS)


def test_rings_for_new_formats(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(b"")
    tgz = tmp_path / "a.tar.gz"
    tgz.write_bytes(b"")
    srt = tmp_path / "a.srt"
    srt.write_text("")
    s = tmp_path / "a.svg"
    s.write_text("")
    r = rings([z])
    assert labels(r["Convert"]) == ["TAR", "TAR.GZ"] and labels(r["Tools"]) == ["Extract Files"]
    assert all(i.key.startswith("convert:") for i in r["Convert"])
    assert labels(rings([tgz])["Convert"]) == ["ZIP", "TAR"]
    assert list(rings([srt])) == ["Convert", "Actions"] and labels(rings([srt])["Convert"]) == ["VTT", "TXT"]
    assert list(rings([s])) == ["Convert", "Actions"] and "PNG" in labels(rings([s])["Convert"])
    assert "Zip" in labels(rings([s])["Actions"])


def test_image_and_pdf_rings_get_the_new_tools(tmp_path):
    img = tmp_path / "p.png"
    Image.new("RGB", (10, 10)).save(img)
    pdf = numbered_pdf(tmp_path / "d.pdf", 1)
    assert labels(rings([img])["Tools"])[-1] == "Read QR"
    assert labels(rings([pdf])["Tools"])[-1] == "Reorder"


class FakeApp:
    """Stands in for DragShiftApp so run_item's routing can be checked without a tray app."""

    def __init__(self):
        self.calls = []
        self.notify = lambda *a, **k: None

    def submit(self, action, srcs, fn, working="", extra="", undo=None):
        self.calls.append(("submit", action, fn()))

    def open_tool(self, name, make):
        self.calls.append(("open", name, make()))

    def run_action(self, name, paths):
        DragShiftApp.run_action(self, name, paths)

    def make_archive(self, fmt, paths):
        DragShiftApp.make_archive(self, fmt, paths)


def test_routing_archive_items_and_zip_action(tmp_path):
    d = tmp_path / "Folder"
    d.mkdir()
    (d / "x.txt").write_text("x")
    f = FakeApp()
    DragShiftApp.run_item(f, Item("TAR.GZ", key="archive:TAR.GZ"), [str(d)])
    kind, action, outs = f.calls[-1]
    assert action == "archive:TAR.GZ" and outs[0].name == "Folder.tar.gz"
    DragShiftApp.run_item(f, Item("Zip", key="action:Zip"), [str(d), str(d / "x.txt")])
    assert f.calls[-1][1] == "archive:ZIP" and f.calls[-1][2][0].name == "Archive.zip"
    DragShiftApp.run_item(f, Item("Extract Files", key="tool:Extract Files"), [str(outs[0])])
    assert f.calls[-1][1] == "tool:Extract Files" and (f.calls[-1][2][0] / "x.txt").read_text() == "x"


def test_routing_windows(tmp_path):
    f = FakeApp()
    pdf = numbered_pdf(tmp_path / "d.pdf", 2)
    DragShiftApp.run_item(f, Item("Reorder", key="tool:Reorder"), [str(pdf)])
    assert isinstance(f.calls[-1][2], ReorderWindow)
    DragShiftApp.run_item(f, Item("Compress", key="tool:Compress"), [str(pdf)])
    assert isinstance(f.calls[-1][2], compress.PercentCompressWindow)
    img = qr_png(tmp_path, "hello")
    DragShiftApp.run_item(f, Item("Read QR", key="tool:Read QR"), [str(img)])
    assert isinstance(f.calls[-1][2], qr.QrWindow)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    DragShiftApp.run_item(f, Item("Compress", key="tool:Compress"), [str(vid)])
    w = f.calls[-1][2]
    assert isinstance(w, compress.PercentCompressWindow) and w.recommended is media.compress
    for _, _, win in f.calls:
        win.close()


def test_routing_subtitle_and_svg_convert(tmp_path):
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    f = FakeApp()
    DragShiftApp.run_item(f, Item("VTT", key="convert:VTT"), [str(srt)])
    assert f.calls[-1][2][0].read_text().startswith("WEBVTT")
    s = svg_file(tmp_path, "i.svg", 'width="10" height="10"', RED)
    DragShiftApp.run_item(f, Item("PNG", key="convert:PNG"), [str(s)])
    assert f.calls[-1][2][0].suffix == ".png"


# ---------------------------------------------------------------- second-launch hand-off
@pytest.mark.parametrize("data", [[1, 2], "files", 3, None, {"files": "/etc/passwd"}, {"files": [1, None]},
                                  {"files": [["a"]]}, {"show": "yes"}, {"files": {"a": 1}}])
def test_ipc_ignores_anything_but_the_expected_message(data):
    assert app.ipc_request(data) is None


def test_ipc_accepts_files_and_show(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert app.ipc_request({"files": [str(f), str(tmp_path / "gone.txt"), "bad\0name"]}) == ([str(f)], False)
    assert app.ipc_request({"show": True}) == ([], True)
    assert app.ipc_request({}) == ([], False)


def test_ipc_server_is_only_for_this_user(tmp_path):
    from PySide6.QtNetwork import QLocalServer
    server = app.listen_ipc(f"DragShift-test-{os.getpid()}")
    try:
        assert server.isListening()
        if sys.platform != "win32":
            assert server.socketOptions() == QLocalServer.UserAccessOption
            assert os.stat(server.fullServerName()).st_mode & 0o077 == 0
    finally:
        server.close()
