import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "MotionPhoto2"))
import video_removal as vr  # noqa: E402


def make(path, data=b"video"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_move_keeps_relative_path_and_handles_collisions(tmp_path):
    a = make(tmp_path / "sub" / "a.MOV", b"1")
    assert vr.remove_video(a, "move", tmp_path) == "move"
    assert not a.exists()
    assert (tmp_path / "_originals" / "sub" / "a.MOV").read_bytes() == b"1"
    b = make(tmp_path / "sub" / "a.MOV", b"2")
    vr.remove_video(b, "move", tmp_path)
    assert (tmp_path / "_originals" / "sub" / "a_1.MOV").read_bytes() == b"2"


def test_custom_relative_folder(tmp_path):
    a = make(tmp_path / "a.mov")
    vr.remove_video(a, "move", tmp_path, "bin")
    assert (tmp_path / "bin" / "a.mov").exists()


def test_trash_failure_falls_back_to_move_not_delete(tmp_path, monkeypatch):
    import send2trash

    def boom(_):
        raise OSError("no trash")
    monkeypatch.setattr(send2trash, "send2trash", boom)
    a = make(tmp_path / "a.mov")
    assert vr.remove_video(a, "trash", tmp_path) == "move"
    assert (tmp_path / "_originals" / "a.mov").exists()


def test_delete_mode(tmp_path):
    a = make(tmp_path / "a.mov")
    assert vr.remove_video(a, "delete", tmp_path) == "delete"
    assert not a.exists()


def test_rejects_outside_root_and_non_video(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = make(tmp_path / "out.mov")
    with pytest.raises(ValueError):
        vr.remove_video(outside, "delete", root)
    assert outside.exists()
    photo = make(root / "a.jpg")
    with pytest.raises(ValueError):
        vr.remove_video(photo, "delete", root)
    assert photo.exists()


def test_normalize_mode_defaults():
    assert vr.normalize_mode("bogus") == vr.default_mode()
    assert vr.normalize_mode(None) == vr.default_mode()
    assert vr.normalize_mode("DELETE") == "delete"


def test_verify_muxed_output(tmp_path):
    video = make(tmp_path / "v.mov", b"VIDEODATA")
    out = make(tmp_path / "o.jpg", b"IMAGE" + b"hdr" + b"VIDEODATA" + b"tail")
    assert vr.verify_muxed_output(out, video, 8, 5) == ""
    assert "differ" in vr.verify_muxed_output(out, video, 9, 5)
    assert "expected at least" in vr.verify_muxed_output(make(tmp_path / "s.jpg", b"IMG"), video, 0, 5)
    assert "outside" in vr.verify_muxed_output(out, video, 100, 1)
    assert "does not exist" in vr.verify_muxed_output(tmp_path / "none", video, 0, 5)


def _jpeg(xmp: bytes, scan: bytes = b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00" + b"PIXELS" * 20 + b"\xff\xd9") -> bytes:
    app1 = b"\xff\xe1" + (len(xmp) + 2).to_bytes(2, "big") + xmp
    dqt = b"\xff\xdb\x00\x04\x00\x00"
    return b"\xff\xd8" + app1 + dqt + scan


def test_jpeg_scan_tail():
    data = _jpeg(b"x" * 30)
    assert vr.jpeg_scan_tail(data).startswith(b"\xff\xda") and vr.jpeg_scan_tail(data).endswith(b"\xff\xd9")
    assert vr.jpeg_scan_tail(b"not a jpeg") is None
    assert vr.jpeg_scan_tail(b"\xff\xd8\xff\xe1\x00\xf0abc") is None      # segment runs past the end
    assert vr.jpeg_scan_tail(b"\xff\xd8\x00\x00\x00\x00\x00\x00") is None


def _muxed(tmp_path, original: bytes, merged: bytes, video: bytes, name="o.jpg"):
    """merged photo + 3 bytes of Samsung tag padding + video + footer."""
    out = make(tmp_path / name, merged + b"PAD" + video + b"FOOTER")
    return out, len(merged) + 3


def test_verify_jpeg_accepts_smaller_xmp(tmp_path):
    video = make(tmp_path / "v.mov", b"VIDEODATA")
    original = _jpeg(b"x" * 400)
    merged = _jpeg(b"y" * 100)                  # the XMP shrank by 300 bytes
    out, offset = _muxed(tmp_path, original, merged, b"VIDEODATA")
    assert len(merged) + 3 + 9 < len(original) + 9 + 3 + 6      # legacy size check would fail ...
    assert "expected at least" in vr.verify_muxed_output(out, video, offset, len(original))
    scan, end = vr.jpeg_scan_tail(original), len(merged)        # ... the parse based one passes
    assert vr.verify_muxed_output(out, video, offset, len(original), scan, end) == ""


def test_verify_jpeg_rejects_damaged_output(tmp_path):
    video = make(tmp_path / "v.mov", b"VIDEODATA")
    original, merged = _jpeg(b"x" * 400), _jpeg(b"y" * 100)
    scan, end = vr.jpeg_scan_tail(original), len(merged)
    # picture data altered
    bad = merged[:-10] + b"X" * 10
    out, offset = _muxed(tmp_path, original, bad, b"VIDEODATA", "a.jpg")
    assert "picture data" in vr.verify_muxed_output(out, video, offset, len(original), scan, end)
    # truncated header (picture data intact but header chain broken)
    broken = b"\xff\xd8\xff\xe1\x00\x10" + b"z" * 5 + vr.jpeg_scan_tail(merged)
    out, offset = _muxed(tmp_path, original, broken, b"VIDEODATA", "b.jpg")
    assert "well formed" in vr.verify_muxed_output(out, video, offset, len(original), scan, len(broken))
    # wrong video bytes
    out, offset = _muxed(tmp_path, original, merged, b"OTHERDATA", "c.jpg")
    assert "differ" in vr.verify_muxed_output(out, video, offset, len(original), scan, end)
    # video cut short
    out = make(tmp_path / "d.jpg", merged + b"PAD" + b"VIDEO")
    assert "outside" in vr.verify_muxed_output(out, video, len(merged) + 3, len(original), scan, end)
    # image end past the video offset
    out, offset = _muxed(tmp_path, original, merged, b"VIDEODATA", "e.jpg")
    assert "inconsistent" in vr.verify_muxed_output(out, video, offset, len(original), scan, offset + 5)
