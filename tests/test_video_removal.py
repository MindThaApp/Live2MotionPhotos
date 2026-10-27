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
