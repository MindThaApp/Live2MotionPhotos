"""Two photos (HEIC + JPEG) that share one video must both convert; the video goes after the last."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")

import motionphoto2 as mp  # noqa: E402
from test_preview_filters import JPEG, FakeET  # noqa: E402


class FakeMuxer:
    calls: list = []

    def __init__(self, image_fpath, video_fpath, delete_video=False, **kw):
        self.img, self.vid, self.delete = image_fpath, video_fpath, delete_video
        self.output_fpath = image_fpath

    def mux(self):
        if not os.path.exists(self.vid):
            raise mp.MuxerInputError("Video file doesn't exist")
        FakeMuxer.calls.append((Path(self.img).name, self.delete))
        if self.delete:
            os.remove(self.vid)


def test_shared_video_deleted_only_after_last_photo(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.HEIC", "a.JPEG", "b.JPEG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    for n in ("a.MOV", "b.MOV"):
        (root / n).write_bytes(b"vid")
    FakeET.ids = {"a.HEIC": "ID-A", "a.JPEG": "ID-A", "a.MOV": "ID-A", "b.JPEG": "ID-B", "b.MOV": "ID-B"}
    FakeMuxer.calls = []
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(mp, "Muxer", FakeMuxer)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--recursive", "--exif-match", "--delete-video"])
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        mp.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "doesn't exist" not in out and '"status": "error"' not in out
    assert sorted(FakeMuxer.calls) == [("a.HEIC", False), ("a.JPEG", True), ("b.JPEG", True)] or \
        sorted(FakeMuxer.calls) == [("a.HEIC", True), ("a.JPEG", False), ("b.JPEG", True)]
    assert not (root / "a.MOV").exists() and not (root / "b.MOV").exists()
