"""Bugs found by the real run: 'wide'-first embedded videos, HEIC post-write check,
two videos for one Live Photo, exiftool's own message for unreadable files."""
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")

import motionphoto2 as mp  # noqa: E402
import utils  # noqa: E402
import video_removal as vr  # noqa: E402
from Muxer import Muxer, MuxerInputError  # noqa: E402
from SamsungTags import SamsungTags  # noqa: E402
from test_preview_filters import JPEG, FakeET  # noqa: E402

# the layout of the real files: 'wide' box, then mdat (no ftyp in the first 16 bytes)
WIDE_VIDEO = b"\x00\x00\x00\x08wide\x00M$\xf9mdat" + bytes(range(256)) * 20
FTYP_VIDEO = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  " + bytes(range(256)) * 20


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def heif(pic: bytes, xmp: bytes = b"", thumb: bytes = b"THUMBDATA") -> bytes:
    """A minimal valid HEIF: items 1 (primary hvc1) and 2 (thumbnail), optional XMP item 3."""
    def build(base):
        data_items = [(1, pic), (2, thumb)] + ([(3, xmp)] if xmp else [])
        infe = b"".join(box(b"infe", bytes([2, 0, 0, 0]) + struct.pack(">HH", i, 0) +
                           (b"mime" if i == 3 else b"hvc1") + b"\x00") for i, _ in data_items)
        iinf = box(b"iinf", b"\x00\x00\x00\x00" + struct.pack(">H", len(data_items)) + infe)
        pitm = box(b"pitm", b"\x00\x00\x00\x00" + struct.pack(">H", 1))
        locs, off = b"", base
        for i, d in data_items:
            locs += struct.pack(">HHH", i, 0, 1) + struct.pack(">II", off, len(d))
            off += len(d)
        iloc = box(b"iloc", b"\x00\x00\x00\x00" + bytes([0x44, 0x00]) + struct.pack(">H", len(data_items)) + locs)
        meta = box(b"meta", b"\x00\x00\x00\x00" + pitm + iinf + iloc)
        return box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic") + meta, data_items
    head, items = build(0)
    head, items = build(len(head) + 8)
    return head + box(b"mdat", b"".join(d for _, d in items))


def with_video(photo: bytes, video: bytes, kind="heic") -> bytes:
    tags = SamsungTags(video, kind)
    tags.set_image_size(len(photo))
    return photo + tags.video_footer()


# ─── bug 1: videos starting with a 'wide' box ────────────────────────────────

@pytest.mark.parametrize("video", [WIDE_VIDEO, FTYP_VIDEO])
@pytest.mark.parametrize("kind", ["heic", "jpeg"])
def test_wide_and_ftyp_videos_are_found(tmp_path, video, kind):
    photo = heif(b"PIC" * 50) if kind == "heic" else JPEG + b"\x00" * 100
    p, v = tmp_path / "p.x", tmp_path / "v.mov"
    p.write_bytes(with_video(photo, video, kind))
    v.write_bytes(video)
    off, ln = vr.find_embedded_video(p, use_xmp=False)
    assert p.read_bytes()[off:off + ln] == video
    assert vr.verify_embedded_video(p, v) == ""
    assert utils.is_motion_photo(str(p), FakeET()) is True


def test_non_video_at_pointer_is_not_found(tmp_path):
    p = tmp_path / "p.jpg"
    p.write_bytes(with_video(JPEG + b"\x00" * 100, b"\x00\x00\x00\x08junk" + bytes(300)))
    assert vr.find_embedded_video(p) is None
    assert vr.find_embedded_video(p, check_signature=False) is not None


def test_muxer_refuses_photo_that_already_has_a_video(tmp_path):
    p, v = tmp_path / "a.heic", tmp_path / "a.mov"
    p.write_bytes(with_video(heif(b"PIC" * 50), WIDE_VIDEO))
    v.write_bytes(FTYP_VIDEO)
    before = p.read_bytes()
    with pytest.raises(MuxerInputError, match="already contains an embedded video"):
        Muxer(str(p), str(v), exiftool=None, overwrite=True).mux()
    assert p.read_bytes() == before and v.exists()


# ─── bug 2: HEIC structural verification ─────────────────────────────────────

def _merged(tmp_path, original: bytes, merged_photo: bytes, video=FTYP_VIDEO):
    out, vid = tmp_path / "out.heic", tmp_path / "v.mov"
    out.write_bytes(with_video(merged_photo, video))
    vid.write_bytes(video)
    tags = SamsungTags(video, "heic")
    return out, vid, len(merged_photo), len(merged_photo) + tags.get_image_padding()


def test_smaller_xmp_passes_structural_check_but_fails_size_check(tmp_path):
    original = heif(b"PIC" * 500, xmp=b"<x>" + b"a" * 2000)
    merged = heif(b"PIC" * 500, xmp=b"<x>" + b"a" * 100)       # exiftool rewrote a smaller XMP
    assert len(merged) < len(original)
    out, vid, end, voff = _merged(tmp_path, original, merged)
    items = vr.heic_image_items(original)
    assert vr.verify_muxed_output(out, vid, voff, len(original), None, end, items) == ""
    assert "expected at least" in vr.verify_muxed_output(out, vid, voff, len(original))   # the old false refusal


def test_changed_picture_data_is_refused(tmp_path):
    original = heif(b"PIC" * 500, xmp=b"<x>")
    items = vr.heic_image_items(original)
    for bad in (heif(b"PIX" + b"PIC" * 499, xmp=b"<x>"), heif(b"PIC" * 500, xmp=b"<x>", thumb=b"THUMBDATB")):
        out, vid, end, voff = _merged(tmp_path, original, bad)
        assert "differs" in vr.verify_muxed_output(out, vid, voff, len(original), None, end, items)


def test_truncated_or_corrupt_output_is_refused(tmp_path):
    original = heif(b"PIC" * 500)
    items = vr.heic_image_items(original)
    out, vid, end, voff = _merged(tmp_path, original, original[:-40])
    assert "well formed" in vr.verify_muxed_output(out, vid, voff, len(original), None, end, items)
    out, vid, end, voff = _merged(tmp_path, original, b"\x00" * len(original))
    assert "well formed" in vr.verify_muxed_output(out, vid, voff, len(original), None, end, items)


def test_embedded_video_must_be_identical(tmp_path):
    original = heif(b"PIC" * 500)
    items = vr.heic_image_items(original)
    out, vid, end, voff = _merged(tmp_path, original, original)
    vid.write_bytes(FTYP_VIDEO[:-1] + b"\x01")
    assert "differ" in vr.verify_muxed_output(out, vid, voff, len(original), None, end, items)


def test_unparseable_original_falls_back_to_size_check(tmp_path):
    with pytest.raises(ValueError):
        vr.heic_image_items(b"not a heic at all" * 10)
    out, vid, end, voff = _merged(tmp_path, b"x" * 100, b"x" * 100)
    assert vr.verify_muxed_output(out, vid, voff, 100, None, end, None) == ""
    assert "expected at least" in vr.verify_muxed_output(out, vid, voff, 10 ** 6, None, end, None)


# ─── bug 3: one Live Photo, two videos ───────────────────────────────────────

class FakeMuxer:
    calls: list = []

    def __init__(self, image_fpath, video_fpath, delete_video=False, **kw):
        self.img, self.vid, self.delete = image_fpath, video_fpath, delete_video
        self.output_fpath, self.video_removed = image_fpath, False

    def mux(self):
        FakeMuxer.calls.append((Path(self.img).name, Path(self.vid).name, self.delete))
        if self.delete:
            Path(self.vid).unlink()
            self.video_removed = True


def run_dir(root, monkeypatch, capsys, ids, *extra):
    FakeET.ids = ids
    FakeMuxer.calls = []
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(mp, "Muxer", FakeMuxer)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--recursive", *extra])
    monkeypatch.setenv("HOME", str(root.parent))
    try:
        mp.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out
    return out, [json.loads(l[9:]) for l in out.splitlines() if l.startswith("@@RESULT ")]


def test_each_photo_takes_its_own_video_by_exif(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("IMG_0001.HEIC", "IMG_0001.JPG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    for n in ("IMG_0001.MOV", "IMG_0001.MP4"):
        (root / n).write_bytes(b"vid" + n.encode())
    ids = {n: "A" for n in ("IMG_0001.HEIC", "IMG_0001.JPG", "IMG_0001.MOV", "IMG_0001.MP4")}
    out, rows = run_dir(root, monkeypatch, capsys, ids, "--exif-match", "--delete-video")
    assert sorted(FakeMuxer.calls) == [("IMG_0001.HEIC", "IMG_0001.MOV", True), ("IMG_0001.JPG", "IMG_0001.MP4", True)] \
        or sorted(FakeMuxer.calls) == [("IMG_0001.HEIC", "IMG_0001.MP4", True), ("IMG_0001.JPG", "IMG_0001.MOV", True)]
    assert not (root / "IMG_0001.MOV").exists() and not (root / "IMG_0001.MP4").exists()
    assert not [r for r in rows if r.get("kind") == "duplicate_video"]
    assert "2 photo(s) converted, 2 video(s) removed, 0 duplicate video(s) kept" in out


def test_extra_duplicate_video_is_kept_and_reported(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "IMG_0001.HEIC").write_bytes(JPEG + b"\x00" * 50)
    for n in ("IMG_0001.MOV", "IMG_0001.MP4"):
        (root / n).write_bytes(b"vid" + n.encode())
    ids = {n: "A" for n in ("IMG_0001.HEIC", "IMG_0001.MOV", "IMG_0001.MP4")}
    out, rows = run_dir(root, monkeypatch, capsys, ids, "--exif-match", "--delete-video")
    assert len(FakeMuxer.calls) == 1
    kept = [r for r in rows if r.get("kind") == "duplicate_video"]
    assert len(kept) == 1 and "duplicate video kept" in kept[0]["reason"] and kept[0]["photo"] == "IMG_0001.HEIC"
    assert (root / kept[0]["file"]).exists()                         # not removed
    assert sorted(p.name for p in root.glob("*.M*")) == [kept[0]["file"]]
    assert "1 photo(s) converted, 1 video(s) removed, 1 duplicate video(s) kept" in out


def test_name_match_uses_both_videos(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("IMG_0001.HEIC", "IMG_0001.JPG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    for n in ("IMG_0001.MOV", "IMG_0001.MP4"):
        (root / n).write_bytes(b"vid" + n.encode())
    out, rows = run_dir(root, monkeypatch, capsys, {}, "--delete-video")
    assert {c[1] for c in FakeMuxer.calls} == {"IMG_0001.MOV", "IMG_0001.MP4"}
    assert not list(root.glob("*.M*"))


def test_shared_single_video_still_removed_once(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.HEIC", "a.JPG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    (root / "a.MOV").write_bytes(b"vid")
    out, rows = run_dir(root, monkeypatch, capsys, {"a.HEIC": "A", "a.JPG": "A", "a.MOV": "A"}, "--exif-match", "--delete-video")
    assert sorted(c[2] for c in FakeMuxer.calls) == [False, True]
    assert "2 photo(s) converted, 1 video(s) removed, 0 duplicate" in out


# ─── unreadable files: exiftool's own message ────────────────────────────────

class ExifFail(Exception):
    def __init__(self, stdout="", stderr=""):
        super().__init__("execute returned a non-zero exit status: 1")
        self.stdout, self.stderr = stdout, stderr


@pytest.mark.parametrize("kw,expected", [
    ({"stderr": "Error: File is empty - x.jpg\n"}, "Error: File is empty - x.jpg"),
    ({"stdout": "Error: File is empty - x.jpg"}, "Error: File is empty - x.jpg"),
    ({"stdout": '[{"SourceFile": "x.jpg", "Error": "File is empty"}]'}, "File is empty"),
    ({}, "execute returned a non-zero exit status: 1"),
])
def test_exiftool_message(kw, expected):
    assert utils.exiftool_message(ExifFail(**kw)) == expected
