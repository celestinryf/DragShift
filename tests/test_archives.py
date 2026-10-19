"""Archives (dragshift/archives.py). Fixtures are built in the test with zipfile/tarfile directly
(never with the code under test), including hostile ones: zip-slip, absolute paths, drive letters,
symlink escapes, hard links outside, zip bombs, encrypted entries and cp437 names. RAR goes through
the system bsdtar; the RAR fixture is written byte by byte (stored, RAR 1.5-4 format), so no RAR
tool is needed, and the test skips when bsdtar is missing."""
import io
import os
import stat
import struct
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest

from dragshift import archives, convert, report
from dragshift.util import UserError, category

needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


# ---------------------------------------------------------------- helpers
def tree(root: Path) -> dict[str, object]:
    """{relative posix path: bytes for files, 'dir' for folders, '-> target' for links}."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = p.read_bytes()
    return out


def listing(root: Path) -> set[str]:
    return {p.name for p in root.iterdir()}


@pytest.fixture
def docs(tmp_path) -> Path:
    """A folder with nested folders, an empty folder, unicode names, junk and a link."""
    d = tmp_path / "src" / "Docs"
    (d / "sub" / "deeper").mkdir(parents=True)
    (d / "empty").mkdir()
    (d / "a.txt").write_bytes(b"alpha")
    (d / "sub" / "Ünïcödé ✓.txt").write_bytes("ü".encode() * 100)
    (d / "sub" / "deeper" / "b.bin").write_bytes(bytes(range(256)) * 50)
    (d / ".DS_Store").write_bytes(b"junk")
    (d / "sub" / "._a.txt").write_bytes(b"resource fork")
    (d / "__MACOSX").mkdir()
    (d / "__MACOSX" / "x").write_bytes(b"junk")
    if sys.platform != "win32":
        os.symlink("sub/deeper/b.bin", d / "link")
    return d


EXPECTED_DOCS = {
    "a.txt": b"alpha",
    "empty": "dir",
    "sub": "dir",
    "sub/deeper": "dir",
    "sub/deeper/b.bin": bytes(range(256)) * 50,
    "sub/Ünïcödé ✓.txt": "ü".encode() * 100,
}


def zip_with(path: Path, entries: list[tuple], **kw) -> Path:
    """entries: (name, data) or (ZipInfo, data)."""
    with zipfile.ZipFile(path, "w", **kw) as z:
        for name, data in entries:
            z.writestr(name, data)
    return path


def zip_link(name: str, target: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


def tar_with(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]], mode="w") -> Path:
    with tarfile.open(path, mode) as t:
        for info, data in members:
            if data is not None:
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            else:
                t.addfile(info)
    return path


def tinfo(name: str, kind=tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    return info


def assert_refused(src: Path, *needles: str):
    """Extraction fails with a readable message, and nothing new appears beside the archive or
    above it (no half-extracted or hidden temp folders left behind)."""
    before_here, before_up = listing(src.parent), listing(src.parent.parent)
    with pytest.raises(UserError) as e:
        archives.extract(src)
    msg = str(e.value)
    for n in needles:
        assert n in msg, msg
    assert listing(src.parent) == before_here
    assert listing(src.parent.parent) == before_up
    return msg


# ---------------------------------------------------------------- names and formats
@pytest.mark.parametrize("name, suffix, label, base", [
    ("Photos.zip", ".zip", "ZIP", "Photos"),
    ("Photos.TAR.GZ", ".tar.gz", "TAR.GZ", "Photos"),
    ("a.b.tgz", ".tgz", "TAR.GZ", "a.b"),
    ("x.tar.bz2", ".tar.bz2", "TAR.BZ2", "x"),
    ("x.tar.xz", ".tar.xz", "TAR.XZ", "x"),
    ("notes.txt.gz", ".gz", "GZ", "notes.txt"),
    ("song.rar", ".rar", "RAR", "song"),
])
def test_names(name, suffix, label, base):
    assert archives.archive_suffix(name) == suffix
    assert archives.format_label(name) == label
    assert archives.base_name(name) == base


def test_hidden_file_called_zip_is_not_an_archive():
    assert archives.archive_suffix(".zip") == "" and category(".zip") is None


def test_every_suffix_is_an_archive_category():
    for suffix in archives.SUFFIXES:
        assert category("x" + suffix) == "archive", suffix
    assert category("x.docx") == "docx" and category("x.epub") == "epub"   # zips with their own category


def test_targets_leave_out_the_own_format():
    assert archives.targets_for(".zip") == ["TAR", "TAR.GZ"]
    assert archives.targets_for(".tar.gz") == ["ZIP", "TAR"]
    assert archives.targets_for(".tgz") == ["ZIP", "TAR"]
    assert archives.targets_for(".rar") == ["ZIP", "TAR", "TAR.GZ"]
    assert convert.targets_for("archive", ".tar") == ["ZIP", "TAR.GZ"]


def test_unique_name_keeps_double_extensions(tmp_path):
    (tmp_path / "A.tar.gz").write_bytes(b"")
    (tmp_path / "A (1).tar.gz").write_bytes(b"")
    assert archives.unique_name(tmp_path, "A", ".tar.gz").name == "A (2).tar.gz"


# ---------------------------------------------------------------- create
@pytest.mark.parametrize("fmt, ext", [("ZIP", ".zip"), ("TAR", ".tar"), ("TAR.GZ", ".tar.gz")])
def test_round_trip_folder(docs, fmt, ext, tmp_path):
    out = archives.create([docs], fmt)
    assert out == docs.parent / f"Docs{ext}"
    dest = archives.extract(out)
    assert dest == docs.parent / "Docs (1)"           # "Docs" is the source folder itself
    got = tree(dest)
    for k, v in EXPECTED_DOCS.items():
        assert got.pop(k) == v, k
    if sys.platform != "win32":
        assert got.pop("link") == "-> sub/deeper/b.bin"
        assert (dest / "link").read_bytes() == EXPECTED_DOCS["sub/deeper/b.bin"]
    assert got == {}


def test_create_without_absolute_links_round_trips_through_every_format(tmp_path):
    d = tmp_path / "Clean"
    (d / "x" / "y").mkdir(parents=True)
    (d / "x" / "y" / "z.txt").write_text("z")
    (d / "keep empty").mkdir()
    for fmt in archives.TARGETS:
        out = archives.create([d], fmt)
        dest = archives.extract(out)
        assert tree(dest) == {"keep empty": "dir", "x": "dir", "x/y": "dir", "x/y/z.txt": b"z"}, fmt


@needs_symlinks
def test_absolute_link_is_stored_as_a_link_but_refused_on_extract(docs):
    """Making the archive never follows a link; extracting refuses one that points outside."""
    os.symlink("/etc", docs / "outside link")
    out = archives.create([docs], "ZIP")
    with zipfile.ZipFile(out) as z:
        assert z.read("Docs/outside link") == b"/etc"
    assert_refused(out, "outside its folder", "outside link")


def test_zip_layout_order_and_junk(docs):
    out = archives.create([docs], "ZIP")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        infos = {i.filename: i for i in z.infolist()}
    expected = ["Docs/", "Docs/a.txt", "Docs/empty/", "Docs/sub/", "Docs/sub/deeper/", "Docs/sub/deeper/b.bin",
                "Docs/sub/Ünïcödé ✓.txt"]
    if sys.platform != "win32":
        expected = ["Docs/", "Docs/a.txt", "Docs/empty/", "Docs/link", "Docs/sub/",
                    "Docs/sub/deeper/", "Docs/sub/deeper/b.bin", "Docs/sub/Ünïcödé ✓.txt"]
        assert stat.S_ISLNK(infos["Docs/link"].external_attr >> 16)
    assert names == expected                       # sorted, folders before their contents
    assert not any(".DS_Store" in n or "__MACOSX" in n or "/._" in n for n in names)
    assert infos["Docs/sub/Ünïcödé ✓.txt"].flag_bits & 0x800   # UTF-8 name flag


def test_create_is_deterministic(docs):
    a = archives.create([docs], "TAR")
    b = archives.create([docs], "TAR")
    with tarfile.open(a) as ta, tarfile.open(b) as tb:
        assert ta.getnames() == tb.getnames()
        assert all(m.uid == 0 and m.uname == "" for m in ta.getmembers())   # no account names


def test_mixed_selection_is_archive_zip_with_clashes_renamed(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "one" / "a.txt").write_text("1")
    (tmp_path / "two" / "a.txt").write_text("2")
    folder = tmp_path / "one" / "Folder"
    folder.mkdir()
    (folder / "f.txt").write_text("f")
    out = archives.create([tmp_path / "one" / "a.txt", tmp_path / "two" / "a.txt", folder], "ZIP")
    assert out == tmp_path / "one" / "Archive.zip"
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["a.txt", "a (1).txt", "Folder/", "Folder/f.txt"]
        assert z.read("a (1).txt") == b"2"
    assert archives.create([tmp_path / "one" / "a.txt"], "ZIP").name == "a.txt.zip"
    assert archives.create([tmp_path / "one" / "a.txt", folder], "ZIP").name == "Archive (1).zip"


def test_top_level_junk_the_user_picked_is_kept(tmp_path):
    f = tmp_path / ".DS_Store"
    f.write_bytes(b"x")
    with zipfile.ZipFile(archives.create([f], "ZIP")) as z:
        assert z.namelist() == [".DS_Store"]


def test_create_never_archives_its_own_output(tmp_path):
    d = tmp_path / "Box"
    d.mkdir()
    (d / "inside.txt").write_text("i")
    out = archives.create([d / "inside.txt", d], "ZIP")       # output lands inside the folder
    assert out == d / "Archive.zip"
    with zipfile.ZipFile(out) as z:
        assert not any("Archive" in n or ".part" in n for n in z.namelist())


def test_create_missing_item_is_a_user_error(tmp_path):
    with pytest.raises(UserError, match="moved or deleted"):
        archives.create([tmp_path / "gone"], "ZIP")


def test_empty_folder_round_trips(tmp_path):
    d = tmp_path / "Nothing"
    d.mkdir()
    out = archives.create([d], "TAR.GZ")
    with tarfile.open(out) as t:
        assert t.getnames() == ["Nothing"]


# ---------------------------------------------------------------- extract
def test_extract_wraps_loose_files_in_a_folder_named_after_the_archive(tmp_path):
    src = zip_with(tmp_path / "Loose.zip", [("a.txt", "a"), ("b/c.txt", "c")])
    dest = archives.extract(src)
    assert dest == tmp_path / "Loose"
    assert tree(dest) == {"a.txt": b"a", "b": "dir", "b/c.txt": b"c"}


def test_extract_drops_a_single_top_folder(tmp_path):
    src = zip_with(tmp_path / "Pack.zip", [("Inner/", ""), ("Inner/a.txt", "a"), ("Inner/b/", "")])
    dest = archives.extract(src)
    assert dest == tmp_path / "Pack"
    assert tree(dest) == {"a.txt": b"a", "b": "dir"}


def test_extract_keeps_a_single_file_as_is(tmp_path):
    src = zip_with(tmp_path / "One.zip", [("only.txt", "x")])
    assert tree(archives.extract(src)) == {"only.txt": b"x"}


def test_extract_twice_never_overwrites(tmp_path):
    src = zip_with(tmp_path / "Twice.zip", [("a.txt", "a")])
    assert archives.extract(src).name == "Twice"
    assert archives.extract(src).name == "Twice (1)"


def test_extract_skips_mac_junk(tmp_path):
    src = zip_with(tmp_path / "Mac.zip", [("a.txt", "a"), ("__MACOSX/._a.txt", "fork"), (".DS_Store", "x"),
                                         ("sub/._b", "fork"), ("sub/b", "b")])
    assert tree(archives.extract(src)) == {"a.txt": b"a", "sub": "dir", "sub/b": b"b"}


def test_extract_keeps_executable_bit(tmp_path):
    info = zipfile.ZipInfo("run.sh")
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o4755) << 16      # setuid must not survive
    src = zip_with(tmp_path / "Exec.zip", [(info, "#!/bin/sh\n")])
    mode = (archives.extract(src) / "run.sh").stat().st_mode
    if sys.platform != "win32":
        assert mode & 0o100 and not mode & stat.S_ISUID


@pytest.mark.parametrize("mode, ext", [("w:gz", ".tar.gz"), ("w:bz2", ".tar.bz2"), ("w:xz", ".tar.xz"),
                                       ("w:gz", ".tgz"), ("w", ".tar")])
def test_extract_every_tar_flavour(tmp_path, mode, ext):
    src = tar_with(tmp_path / f"T{ext}", [(tinfo("T", tarfile.DIRTYPE), None), (tinfo("T/a.txt"), b"a"),
                                          (tinfo("T/e", tarfile.DIRTYPE), None)], mode)
    assert tree(archives.extract(src)) == {"a.txt": b"a", "e": "dir"}


@pytest.mark.parametrize("module, ext", [("gzip", ".gz"), ("bz2", ".bz2"), ("lzma", ".xz")])
def test_single_compressed_file_is_decompressed_beside_it(tmp_path, module, ext):
    import importlib
    mod = importlib.import_module(module)
    data = b"line\n" * 1000
    src = tmp_path / f"notes.txt{ext}"
    src.write_bytes(mod.compress(data))
    out = archives.extract(src)
    assert out == tmp_path / "notes.txt" and out.read_bytes() == data
    assert archives.extract(src).name == "notes (1).txt"


def test_tar_hard_link_is_copied_inside(tmp_path):
    src = tar_with(tmp_path / "H.tar", [(tinfo("a.txt"), b"data"), (tinfo("b.txt", tarfile.LNKTYPE, "a.txt"), None)])
    dest = archives.extract(src)
    assert tree(dest) == {"a.txt": b"data", "b.txt": b"data"}
    assert (dest / "a.txt").stat().st_ino != (dest / "b.txt").stat().st_ino


def hard_link_bomb(folder: Path, links: int = 5) -> Path:
    """One 2000-byte file and `links` hard links to it (the last ones through the first link):
    tiny on disk, big once copied out."""
    folder.mkdir(exist_ok=True)
    members = [(tinfo("big"), b"x" * 2000), (tinfo("h0", tarfile.LNKTYPE, "big"), None)]
    members += [(tinfo(f"h{i}", tarfile.LNKTYPE, "h0"), None) for i in range(1, links)]
    return tar_with(folder / "Links.tar", members)


def test_tar_hard_links_count_toward_the_size_cap(tmp_path, monkeypatch):
    src = hard_link_bomb(tmp_path / "box")
    with archives._reader(src) as entries:
        assert archives.plan(entries(), src, True).total == 6 * 2000   # also what the space check sees
    monkeypatch.setattr(archives, "MAX_TOTAL", 5000)
    assert_refused(src, "zip bomb")


def test_tar_hard_link_copies_are_capped_while_writing(tmp_path, monkeypatch):
    """Even past the plan, every byte a hard link copies counts against the cap."""
    src = hard_link_bomb(tmp_path / "box")
    monkeypatch.setattr(archives, "plan", lambda entries, src, strip: archives.Plan(0))
    monkeypatch.setattr(archives, "MAX_TOTAL", 5000)
    assert_refused(src, "zip bomb")


def test_tar_devices_and_fifos_are_skipped(tmp_path):
    src = tar_with(tmp_path / "D.tar", [(tinfo("a.txt"), b"a"), (tinfo("fifo", tarfile.FIFOTYPE), None),
                                        (tinfo("dev", tarfile.CHRTYPE), None)])
    assert tree(archives.extract(src)) == {"a.txt": b"a"}


@needs_symlinks
def test_inside_links_are_kept(tmp_path):
    src = zip_with(tmp_path / "L.zip", [("d/a.txt", "a"), (zip_link("d/l", "a.txt"), "a.txt"),
                                        (zip_link("up", "d/a.txt"), "d/a.txt")])
    dest = archives.extract(src)
    assert os.readlink(dest / "d" / "l") == "a.txt" and (dest / "up").read_text() == "a"


# ---------------------------------------------------------------- hostile archives
@pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt", "/etc/evil.txt", "C:/evil.txt",
                                  "C:evil.txt", "..\\evil.txt", "a\\..\\..\\evil.txt", "//server/share/x"])
def test_zip_slip_is_refused(tmp_path, name):
    box = tmp_path / "box"
    box.mkdir()
    src = zip_with(box / "Slip.zip", [("fine.txt", "ok"), (name, "evil")])
    assert_refused(src, "outside its folder")
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.txt"])
def test_tar_slip_is_refused(tmp_path, name):
    box = tmp_path / "box"
    box.mkdir()
    src = tar_with(box / "Slip.tar.gz", [(tinfo("ok.txt"), b"ok"), (tinfo(name), b"evil")], "w:gz")
    assert_refused(src, "outside its folder")


@needs_symlinks
@pytest.mark.parametrize("target", ["../../outside", "/etc/passwd", "../x", "C:/Windows"])
def test_symlink_escape_is_refused(tmp_path, target):
    box = tmp_path / "box"
    box.mkdir()
    src = zip_with(box / "Link.zip", [("a.txt", "a"), (zip_link("l", target), target)])
    assert_refused(src, "outside its folder")


@needs_symlinks
def test_symlink_chain_escape_is_refused(tmp_path):
    """Each link alone looks harmless on paper ('.' then 'a/..'), together they leave the
    folder: only the real-path check after making the link catches this."""
    box = tmp_path / "box"
    box.mkdir()
    src = tar_with(box / "Chain.tar", [(tinfo("x.txt"), b"x"), (tinfo("a", tarfile.SYMTYPE, "."), None),
                                       (tinfo("b", tarfile.SYMTYPE, "a/.."), None)])
    assert_refused(src, "outside its folder")


@needs_symlinks
def test_file_written_through_a_link_is_impossible(tmp_path):
    """A link to a folder outside, then a file "through" it: refused before anything is written."""
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tar_with(box / "Through.tar", [(tinfo("l", tarfile.SYMTYPE, str(outside)), None),
                                         (tinfo("l/pwned.txt"), b"x")])
    assert_refused(src, "outside its folder")
    assert list(outside.iterdir()) == []


def test_hard_link_outside_is_refused(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    src = tar_with(box / "Hard.tar", [(tinfo("h", tarfile.LNKTYPE, "../../etc/passwd"), None)])
    assert_refused(src, "outside its folder")


def test_zip_bomb_by_size_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_TOTAL", 1024 * 1024)
    box = tmp_path / "box"
    box.mkdir()
    src = zip_with(box / "Bomb.zip", [("zeros.bin", b"\0" * (2 * 1024 * 1024))], compression=zipfile.ZIP_DEFLATED)
    assert src.stat().st_size < 10_000
    assert_refused(src, "zip bomb")


def test_zip_bomb_that_lies_about_its_size_is_stopped_while_writing(tmp_path, monkeypatch):
    """A single .gz states no size: the cap applies to what's really written."""
    import gzip
    monkeypatch.setattr(archives, "MAX_TOTAL", 1024 * 1024)
    box = tmp_path / "box"
    box.mkdir()
    src = box / "bomb.bin.gz"
    src.write_bytes(gzip.compress(b"\0" * (3 * 1024 * 1024)))
    assert_refused(src, "zip bomb")


def test_zip_bomb_by_count_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_ENTRIES", 50)
    box = tmp_path / "box"
    box.mkdir()
    src = zip_with(box / "Many.zip", [(f"f{i}.txt", "") for i in range(51)])
    assert_refused(src, "more than 50 items")
    tsrc = tar_with(box / "Many.tar", [(tinfo(f"f{i}"), b"") for i in range(51)])
    assert_refused(tsrc, "more than 50 items")


def test_not_enough_disk_space_is_a_user_error(tmp_path, monkeypatch):
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(archives.shutil, "disk_usage", lambda p: usage(100, 90, 10))
    box = tmp_path / "box"
    box.mkdir()
    src = zip_with(box / "Big.zip", [("a.bin", b"x" * 1000)])
    assert_refused(src, "free space")


def test_encrypted_zip_says_so(tmp_path):
    src = zip_with(tmp_path / "Secret.zip", [("a.txt", "plain")])
    raw = bytearray(src.read_bytes())
    # set bit 0 (encrypted) of the general-purpose flags in the local and central headers
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = raw.index(sig)
        raw[i + off] |= 0x01
    src.write_bytes(bytes(raw))
    assert_refused(src, "password-protected", "Secret.zip")


def test_damaged_archives_are_user_errors(tmp_path):
    for name, data in (("bad.zip", b"PK\x03\x04 not really"), ("bad.tar.gz", b"\x1f\x8b garbage"),
                       ("bad.gz", b"not gzip at all"), ("bad.tar", b"x" * 100)):
        src = tmp_path / name
        src.write_bytes(data)
        with pytest.raises(UserError, match="damaged"):
            archives.extract(src)
    assert {p.name for p in tmp_path.iterdir()} == {"bad.zip", "bad.tar.gz", "bad.gz", "bad.tar"}


def _patch_name(src: Path, placeholder: bytes, real: bytes):
    """Swap a same-length placeholder name for raw bytes (the UTF-8 flag stays off)."""
    raw = src.read_bytes()
    assert len(placeholder) == len(real) and raw.count(placeholder) == 2
    src.write_bytes(raw.replace(placeholder, real))


def test_cp437_names_without_the_utf8_flag(tmp_path):
    src = zip_with(tmp_path / "Old.zip", [("cafX.txt", "c")])
    _patch_name(src, b"cafX.txt", "café.txt".encode("cp437"))      # é is one byte (0x82) in cp437
    assert tree(archives.extract(src)) == {"café.txt": b"c"}


def test_utf8_names_without_the_flag(tmp_path):
    src = zip_with(tmp_path / "Mac.zip", [("XXXXXX.txt", "n")])
    _patch_name(src, b"XXXXXX.txt", "日本.txt".encode("utf-8"))       # 6 bytes of UTF-8
    assert tree(archives.extract(src)) == {"日本.txt": b"n"}


# ---------------------------------------------------------------- convert between formats
def test_zip_to_tar_gz_and_back_keeps_structure(tmp_path):
    src = zip_with(tmp_path / "Set.zip", [("Set/", ""), ("Set/a.txt", "a"), ("Set/empty/", ""),
                                          ("Set/Ünï/b.txt", "b"), ("__MACOSX/._x", "junk")])
    tgz = convert.convert(src, "archive", "TAR.GZ")[0]
    assert tgz.name == "Set.tar.gz"
    with tarfile.open(tgz) as t:
        assert t.getnames() == ["Set", "Set/a.txt", "Set/empty", "Set/Ünï", "Set/Ünï/b.txt"]
    back = archives.convert(tgz, "ZIP")
    assert back.name == "Set (1).zip"
    with zipfile.ZipFile(back) as z:
        assert z.namelist() == ["Set/", "Set/a.txt", "Set/empty/", "Set/Ünï/", "Set/Ünï/b.txt"]


def test_convert_refuses_hostile_archives_too(tmp_path):
    src = zip_with(tmp_path / "Slip.zip", [("../evil.txt", "e")])
    with pytest.raises(UserError, match="outside its folder"):
        archives.convert(src, "TAR")
    assert {p.name for p in tmp_path.iterdir()} == {"Slip.zip"}


def test_single_gz_converts_to_zip(tmp_path):
    import gzip
    src = tmp_path / "log.txt.gz"
    src.write_bytes(gzip.compress(b"hello"))
    out = archives.convert(src, "ZIP")
    with zipfile.ZipFile(out) as z:
        assert out.name == "log.txt.zip" and z.namelist() == ["log.txt"] and z.read("log.txt") == b"hello"


def test_empty_archive_conversion_is_a_user_error(tmp_path):
    src = zip_with(tmp_path / "Empty.zip", [])
    with pytest.raises(UserError, match="empty"):
        archives.convert(src, "TAR")


# ---------------------------------------------------------------- RAR
def _rar_header(htype: int, flags: int, body: bytes) -> bytes:
    rest = struct.pack("<BHH", htype, flags, 7 + len(body)) + body
    return struct.pack("<H", zlib.crc32(rest) & 0xFFFF) + rest


def make_rar(path: Path, files: list[tuple[str, bytes]]) -> Path:
    """A stored (uncompressed) RAR 1.5-4 archive: marker, main header, file headers, end."""
    out = b"Rar!\x1a\x07\x00" + _rar_header(0x73, 0, b"\0" * 6)
    for name, data in files:
        n = name.encode()
        body = struct.pack("<IIBIIBBHI", len(data), len(data), 3, zlib.crc32(data), 0x5A210000, 20, 0x30,
                           len(n), 0o100644) + n
        out += _rar_header(0x74, 0x8000, body) + data
    path.write_bytes(out + _rar_header(0x7B, 0x4000, b""))
    return path


needs_bsdtar = pytest.mark.skipif(archives.bsdtar() is None, reason="bsdtar (libarchive) not installed")


@needs_bsdtar
def test_rar_extracts_through_bsdtar(tmp_path):
    src = make_rar(tmp_path / "Album.rar", [("Album/a.txt", b"hello rar"), ("Album/Ünï.txt", b"u")])
    dest = archives.extract(src)
    assert dest == tmp_path / "Album"
    assert tree(dest) == {"a.txt": b"hello rar", "Ünï.txt": b"u"}


@needs_bsdtar
def test_rar_to_zip(tmp_path):
    src = make_rar(tmp_path / "R.rar", [("x/y.txt", b"y")])
    with zipfile.ZipFile(archives.convert(src, "ZIP")) as z:
        assert z.namelist() == ["x/", "x/y.txt"]


@needs_bsdtar
def test_rar_slip_is_refused(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    src = make_rar(box / "Evil.rar", [("ok.txt", b"ok"), ("../evil.txt", b"evil")])
    assert_refused(src, "outside its folder")
    assert not (tmp_path / "evil.txt").exists()


@needs_bsdtar
def test_damaged_rar_is_a_user_error(tmp_path):
    src = tmp_path / "Broken.rar"
    src.write_bytes(b"Rar!\x1a\x07\x00 this is not a rar")
    with pytest.raises(UserError):
        archives.extract(src)
    assert {p.name for p in tmp_path.iterdir()} == {"Broken.rar"}


def test_rar_without_bsdtar_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "bsdtar", lambda: None)
    src = make_rar(tmp_path / "R.rar", [("a", b"a")])
    with pytest.raises(UserError, match="bsdtar"):
        archives.extract(src)


def test_rar_tool_is_called_with_an_argument_list(tmp_path, monkeypatch):
    seen = {}

    class FakeProc:
        def __init__(self, args, **kw):
            seen["args"], seen["kw"] = args, kw
            self.stdout = io.BytesIO(b"")
            self.returncode = 1

        def wait(self):
            return 1

        def poll(self):
            return 1

    monkeypatch.setattr(archives.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(archives, "bsdtar", lambda: "/usr/bin/bsdtar")
    src = make_rar(tmp_path / "-rf weird; name.rar", [("a", b"a")])
    with pytest.raises(UserError, match="damaged"):
        archives.extract(src)
    assert seen["args"][:5] == ["/usr/bin/bsdtar", "-c", "-f", "-", "--format"]
    assert seen["args"][-1] == "@" + str(src.resolve())       # one argument, absolute: never an option
    assert "shell" not in seen["kw"]


# ---------------------------------------------------------------- progress, cancel, report
def test_cancel_stops_extraction_and_cleans_up(tmp_path):
    import threading
    from dragshift import media
    box = tmp_path / "box"
    box.mkdir()
    src = zip_with(box / "Big.zip", [(f"f{i}.bin", os.urandom(4096)) for i in range(5)])
    cancel = threading.Event()
    cancel.set()
    with media.reporting(None, cancel), pytest.raises(media.Cancelled):
        archives.extract(src)
    assert listing(box) == {"Big.zip"}


def test_progress_is_reported(tmp_path):
    from dragshift import media
    src = zip_with(tmp_path / "P.zip", [(f"f{i}.bin", os.urandom(50_000)) for i in range(4)])
    seen = []
    with media.reporting(seen.append, None):
        archives.extract(src)
    assert seen and seen[-1] == pytest.approx(1.0) and seen == sorted(seen)


def test_report_summaries(docs, tmp_path):
    out = archives.create([docs], "ZIP")
    r = report.summarize("archive:ZIP", [docs], [out])
    assert r.title == "Compressed “Docs”"
    assert r.detail.startswith("Docs.zip · ")
    dest = archives.extract(out)
    r = report.summarize("tool:Extract Files", [out], [dest])
    assert r.title.startswith("Extracted ") and r.detail == f"{dest.name}/"
    tgz = archives.convert(out, "TAR.GZ")
    r = report.summarize("convert:TAR.GZ", [out], [tgz])
    assert r.title == "ZIP → TAR.GZ"
    r = report.summarize("convert:ZIP", [tgz], [archives.convert(tgz, "ZIP")])
    assert r.title == "TAR.GZ → ZIP"


def test_count_files(docs):
    n = archives.count_files([docs])
    expected = 6 if sys.platform == "win32" else 7      # incl. junk on disk and the link
    assert n == expected


def test_long_names_give_a_readable_error_or_work(tmp_path):
    src = tmp_path / ("a" * 246 + ".srt")
    src.write_text("x")
    with pytest.raises(UserError, match="name is too long"):
        archives.create([src], "TAR.GZ")      # 'aaa….srt.tar.gz' is past 255 characters
    z = zip_with(tmp_path / ("b" * 240 + ".zip"), [("x.txt", "x")])
    assert tree(archives.extract(z)) == {"x.txt": b"x"}   # the temp folder name stays short
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([src.name, z.name, "b" * 240])
