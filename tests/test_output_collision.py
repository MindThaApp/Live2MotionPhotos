"""A conversion must never overwrite a file that is not the photo being replaced."""
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
from Muxer import Muxer, OutputConflictError  # noqa: E402
from test_preview_filters import JPEG, FakeET  # noqa: E402
from test_safe_overwrite import MP4, StubExif  # noqa: E402


class JpgET(FakeET):
    """Every image is a real JPEG, so IMG.jpeg is written as IMG.jpg."""
    def get_metadata(self, files):
        out = super().get_metadata(files)
        for m, f in zip(out, files):
            m["File:FileTypeExtension"] = "jpg" if Path(f).suffix.lower() in (".jpg", ".jpeg") else "heic"
        return out


def run_main(monkeypatch, capsys, root, *extra):
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", JpgET)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--recursive", *extra])
    monkeypatch.setenv("HOME", str(root.parent))
    try:
        mp.main()
    except SystemExit:
        pass
    rows = {}
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("@@RESULT "):
            r = json.loads(line[9:])
            rows[r["file"]] = r
    return rows


def test_muxer_refuses_to_replace_a_sibling_jpg(tmp_path):
    img, sib, vid = tmp_path / "IMG.jpeg", tmp_path / "IMG.jpg", tmp_path / "IMG.mp4"
    img.write_bytes(JPEG)
    sib.write_bytes(b"SIBLING")
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif("jpg"), overwrite=True, no_xmp=True, video_removal_mode="trash",
              video_removal_root=str(tmp_path))
    with pytest.raises(OutputConflictError, match="would overwrite"):
        m.mux()
    assert sib.read_bytes() == b"SIBLING" and img.read_bytes() == JPEG
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".mp2")]


def test_muxer_refuses_in_delete_mode_too(tmp_path):
    img, sib, vid = tmp_path / "IMG.jpeg", tmp_path / "IMG.jpg", tmp_path / "IMG.mp4"
    img.write_bytes(JPEG)
    sib.write_bytes(b"SIBLING")
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif("jpg"), overwrite=True, no_xmp=True, video_removal_mode="delete")
    with pytest.raises(OutputConflictError):
        m.mux()
    assert sib.read_bytes() == b"SIBLING" and img.exists()


def test_extension_change_without_a_sibling_still_works(tmp_path):
    img, vid = tmp_path / "IMG.jpeg", tmp_path / "IMG.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif("jpg"), overwrite=True, no_xmp=True, video_removal_mode="delete")
    m.mux()
    assert (tmp_path / "IMG.jpg").exists() and not img.exists()


def test_output_dir_existing_other_file_refused_but_rerun_allowed(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    (out / "a.jpg").write_bytes(b"SOMETHING ELSE")
    with pytest.raises(OutputConflictError):
        Muxer(str(img), str(vid), StubExif("jpg"), output_directory=str(out), no_xmp=True).mux()
    assert (out / "a.jpg").read_bytes() == b"SOMETHING ELSE"
    (out / "a.jpg").unlink()
    Muxer(str(img), str(vid), StubExif("jpg"), output_directory=str(out), no_xmp=True).mux()
    first = (out / "a.jpg").read_bytes()
    Muxer(str(img), str(vid), StubExif("jpg"), output_directory=str(out), no_xmp=True).mux()   # same conversion again
    assert (out / "a.jpg").read_bytes() == first


def test_two_sources_one_output_in_a_run(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    vid = tmp_path / "a.mp4"
    vid.write_bytes(MP4)
    claimed = set()
    for name in ("a.jpeg", "a.jpg"):
        (tmp_path / name).write_bytes(JPEG)
    Muxer(str(tmp_path / "a.jpeg"), str(vid), StubExif("jpg"), output_directory=str(out), no_xmp=True,
          claimed_outputs=claimed).mux()
    with pytest.raises(OutputConflictError, match="also writes"):
        Muxer(str(tmp_path / "a.jpg"), str(vid), StubExif("jpg"), output_directory=str(out), no_xmp=True,
              claimed_outputs=claimed).mux()


def make_dir(tmp_path):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("IMG_1.jpeg", "IMG_1.jpg", "IMG_1.HEIC"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    (root / "IMG_1.MOV").write_bytes(MP4)
    return root


def _fs_case_insensitive(path):
    probe = Path(path) / "CaseProbe.tmp"
    probe.write_bytes(b"x")
    try:
        return (Path(path) / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_preview_flags_the_conflict(tmp_path, monkeypatch, capsys):
    if _fs_case_insensitive(tmp_path):
        pytest.skip("needs a case-sensitive filesystem")
    root = make_dir(tmp_path)
    rows = run_main(monkeypatch, capsys, root, "--dry-run", "--overwrite")
    assert rows["IMG_1.jpeg"]["status"] == "skipped" and rows["IMG_1.jpeg"]["kind"] == "conflict"
    assert "would overwrite" in rows["IMG_1.jpeg"]["reason"]
    assert rows["IMG_1.jpg"]["status"] == "planned" and rows["IMG_1.HEIC"]["status"] == "planned"
    assert not list(root.glob(".mp2*"))


def test_two_photos_one_output_flagged_in_preview(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.jpeg", "b.jpg"):
        (root / n).write_bytes(JPEG)
    for n in ("a.mov", "b.mov"):
        (root / n).write_bytes(MP4)
    # LIVE-suffix names differ, so use an output dir where nothing collides: control case
    rows = run_main(monkeypatch, capsys, root, "--dry-run", "--output-directory", str(tmp_path / "out"))
    assert all(r["status"] == "planned" for r in rows.values())


class RecMuxer:
    calls: list = []

    def __init__(self, image_fpath, video_fpath, delete_video=False, **kw):
        self.img, self.vid, self.delete = image_fpath, video_fpath, delete_video
        self.output_fpath = image_fpath

    def mux(self):
        if not os.path.exists(self.vid):
            raise mp.MuxerInputError("Video file doesn't exist")
        RecMuxer.calls.append((Path(self.img).name, self.delete))
        if self.delete:
            os.remove(self.vid)


def test_name_match_shares_one_video_between_photos(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("IMG_1.HEIC", "IMG_1.jpeg", "IMG_1.jpg", "IMG_2.jpg"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    for n in ("IMG_1.MOV", "IMG_2.MOV"):
        (root / n).write_bytes(b"vid")
    RecMuxer.calls = []
    monkeypatch.setattr(mp, "Muxer", RecMuxer)
    rows = run_main(monkeypatch, capsys, root, "--delete-video")
    assert all(r["status"] == "converted" for r in rows.values()), rows
    deletes = {n: d for n, d in RecMuxer.calls}
    assert sorted(n for n, d in RecMuxer.calls if n.startswith("IMG_1")) == ["IMG_1.HEIC", "IMG_1.jpeg", "IMG_1.jpg"]
    assert sum(d for n, d in RecMuxer.calls if n.startswith("IMG_1")) == 1     # only the last photo deletes it
    assert deletes["IMG_2.jpg"] is True
    assert not (root / "IMG_1.MOV").exists() and not (root / "IMG_2.MOV").exists()


def test_name_match_copy_unmuxed_does_not_copy_matched_video(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.jpg", "a.heic"):
        (root / n).write_bytes(JPEG)
    (root / "a.mov").write_bytes(b"vid")
    RecMuxer.calls = []
    monkeypatch.setattr(mp, "Muxer", RecMuxer)
    run_main(monkeypatch, capsys, root, "--copy-unmuxed", "--output-directory", str(tmp_path / "out"))
    assert not (tmp_path / "out" / "a.mov").exists()
    assert len(RecMuxer.calls) == 2


def test_video_of_a_skipped_conflict_photo_is_kept(tmp_path, monkeypatch, capsys):
    root = make_dir(tmp_path)
    rows = run_main(monkeypatch, capsys, root, "--overwrite", "--delete-video", "--no-xmp", "--video-removal-mode", "move")
    assert rows["IMG_1.jpeg"]["kind"] == "conflict"
    assert (root / "IMG_1.MOV").exists()
