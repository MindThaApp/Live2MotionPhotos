"""An empty or corrupt file must not abort an exif-match run: it is skipped, untouched, the rest converts."""
import json
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


class ExifFail(Exception):
    def __init__(self, stderr):
        super().__init__("execute returned a non-zero exit status: 1")
        self.stderr = stderr


class BadET(FakeET):
    """Like exiftool: a batch containing an empty or corrupt file fails as a whole."""
    bad = ("empty.jpg", "corrupt.jpg", "bad.mov")

    def get_metadata(self, files):
        if isinstance(files, (str, os.PathLike)):
            files = [files]
        for f in files:
            if Path(f).name in self.bad:
                raise ExifFail("Error: File is empty - " + str(f))
        return super().get_metadata(files)


class FakeMuxer:
    calls: list = []

    def __init__(self, image_fpath, video_fpath, **kw):
        self.img, self.output_fpath = image_fpath, image_fpath

    def mux(self):
        FakeMuxer.calls.append(Path(self.img).name)


def run(root, monkeypatch, capsys, *extra):
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", BadET)
    monkeypatch.setattr(mp, "Muxer", FakeMuxer)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--recursive", "--exif-match", *extra])
    monkeypatch.setenv("HOME", str(root.parent))
    FakeMuxer.calls = []
    try:
        mp.main()
    except SystemExit as e:
        assert e.code in (None, 0)
    out = capsys.readouterr().out
    return [json.loads(l[9:]) for l in out.splitlines() if l.startswith("@@RESULT ")]


def test_empty_and_corrupt_files_are_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "empty.jpg").write_bytes(b"")
    (root / "corrupt.jpg").write_bytes(b"\x00junk")
    (root / "bad.mov").write_bytes(b"\x00junk")
    for n in ("a.jpg", "b.jpg"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    for n in ("a.mov", "b.mov"):
        (root / n).write_bytes(b"vid")
    BadET.ids = {"a.jpg": "A", "a.mov": "A", "b.jpg": "B", "b.mov": "B"}
    rows = {r["file"]: r for r in run(root, monkeypatch, capsys)}
    assert sorted(FakeMuxer.calls) == ["a.jpg", "b.jpg"]
    for n in ("empty.jpg", "corrupt.jpg", "bad.mov"):
        assert rows[n]["status"] == "skipped" and rows[n]["reason"].startswith("unreadable:")
        assert "File is empty" in rows[n]["reason"]
        assert (root / n).exists()
    assert (root / "empty.jpg").stat().st_size == 0


def test_dry_run_survives_unreadable(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "empty.jpg").write_bytes(b"")
    (root / "a.jpg").write_bytes(JPEG + b"\x00" * 50)
    (root / "a.mov").write_bytes(b"vid")
    BadET.ids = {"a.jpg": "A", "a.mov": "A"}
    rows = {r["file"]: r for r in run(root, monkeypatch, capsys, "--dry-run")}
    assert rows["a.jpg"]["kind"] == "ready"
    assert rows["empty.jpg"]["reason"].startswith("unreadable:")
    assert FakeMuxer.calls == []


def test_muxer_wraps_exiftool_failure(tmp_path):
    from Muxer import Muxer, MuxerInputError

    class ET:
        def get_metadata(self, files):
            raise ExifFail("Error: File is empty")

    img, vid = tmp_path / "x.jpg", tmp_path / "x.mov"
    img.write_bytes(b"")
    vid.write_bytes(b"vid")
    with pytest.raises(MuxerInputError, match="cannot read"):
        Muxer(image_fpath=str(img), video_fpath=str(vid), exiftool=ET()).mux()
    assert img.exists() and vid.exists()
