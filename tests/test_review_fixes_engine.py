"""Regression tests for the 2026-11-09 review findings that live in the converter
(MotionPhoto2/): every test works in a pytest temp dir only."""
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

import extract as ex  # noqa: E402
import motionphoto2 as mp  # noqa: E402
import utils  # noqa: E402
import video_removal as vr  # noqa: E402
from Muxer import Muxer, MuxerInputError, OutputConflictError  # noqa: E402
from test_preview_filters import JPEG, VIDEO, FakeET  # noqa: E402
from test_safe_overwrite import MP4, StubExif  # noqa: E402


def run_cli(monkeypatch, capsys, *args, et=FakeET):
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", et)
    monkeypatch.setattr(sys, "argv", ["mp", *args])
    code = 0
    try:
        mp.main()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    rows = {}
    for line in out.splitlines():
        if line.startswith("@@RESULT "):
            r = json.loads(line[9:])
            rows[r["file"]] = r
    return code, out, rows


# ── 1. output folder == input folder ───────────────────────────────────────────

def test_output_conflict_source_itself_only_with_overwrite(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(JPEG)
    assert "source photo itself" in utils.output_conflict(str(src), str(src))
    assert utils.output_conflict(str(src), str(src), overwrite=True) is None


def test_muxer_refuses_output_directory_equal_to_input_directory(tmp_path):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif("jpg"), output_directory=str(tmp_path), no_xmp=True)
    with pytest.raises(OutputConflictError, match="source photo itself"):
        m.mux()
    assert img.read_bytes() == JPEG
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "a.mp4"]


def test_muxer_refuses_output_file_equal_to_source_without_overwrite(tmp_path):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif("jpg"), output_fpath=str(img), no_xmp=True)
    with pytest.raises(OutputConflictError):
        m.mux()
    assert img.read_bytes() == JPEG


def test_cli_rejects_output_directory_equal_to_input(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    (root / "a.mov").write_bytes(VIDEO)
    code, out, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--output-directory", str(root))
    assert code == 1 and "same as input directory" in out
    assert (root / "a.jpg").read_bytes() == JPEG and sorted(p.name for p in root.iterdir()) == ["a.jpg", "a.mov"]


def test_dry_run_shows_source_output_as_conflict_not_convert(tmp_path, capsys):
    img = tmp_path / "a.jpg"
    img.write_bytes(JPEG)

    class Args:
        overwrite, delete_video, dry_run = False, False, True
    mp.report_dry_run(Args, "a.jpg", str(img), str(tmp_path / "a.mov"), str(tmp_path),
                      {"video_removal_mode": "move"})
    out = capsys.readouterr().out
    assert "Would convert" not in out
    row = json.loads(next(line for line in out.splitlines() if line.startswith("@@RESULT "))[9:])
    assert row["kind"] == "conflict" and row["status"] == "skipped"


# ── 5. line breaks in file names ───────────────────────────────────────────────

def test_muxer_refuses_newline_names(tmp_path):
    img, vid = tmp_path / "a\nb.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    with pytest.raises(MuxerInputError, match="line break"):
        Muxer(str(img), str(vid), StubExif("jpg"), no_xmp=True)
    img2, vid2 = tmp_path / "c.jpg", tmp_path / "c\r.mp4"
    img2.write_bytes(JPEG)
    vid2.write_bytes(MP4)
    with pytest.raises(MuxerInputError, match="line break"):
        Muxer(str(img2), str(vid2), StubExif("jpg"), no_xmp=True)


def test_scanner_skips_and_reports_newline_names_before_exiftool(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    bad = "x\n-overwrite_original\n.jpg"
    (root / bad).write_bytes(JPEG)
    (root / "x.mov").write_bytes(VIDEO)
    seen = []

    class SpyET(FakeET):
        def get_metadata(self, files):
            seen.extend([files] if isinstance(files, (str, os.PathLike)) else files)
            return super().get_metadata([files] if isinstance(files, (str, os.PathLike)) else files)

        def execute(self, *a, **k):
            seen.extend(a)
            return super().execute(*a, **k)

    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    for extra in ((), ("--exif-match",)):
        code, out, rows = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--dry-run", *extra, et=SpyET)
        assert rows[bad]["status"] == "error" and "line break" in rows[bad]["reason"]
        assert not any("\n" in str(x) or "\r" in str(x) for x in seen)


def test_get_metadata_safe_never_passes_newline_paths(tmp_path):
    calls = []

    class ET:
        def get_metadata(self, files):
            calls.append(files)
            return [{"ok": 1}] * (len(files) if isinstance(files, list) else 1)

    metas, errors = mp.get_metadata_safe(ET(), ["/a/good.jpg", "/a/ba\nd.jpg", "/a/good2.jpg"])
    assert errors == {1: vr.LINE_BREAK_REASON} and metas[1] == {} and metas[0] == {"ok": 1}
    assert all("\n" not in p for c in calls for p in (c if isinstance(c, list) else [c]))


def test_is_motion_photo_and_extract_refuse_newline_paths(tmp_path):
    p = tmp_path / "a\nb.jpg"
    p.write_bytes(JPEG)
    with pytest.raises(ValueError):
        utils.is_motion_photo(str(p), object())
    with pytest.raises(ex.ExtractError, match="line break"):
        ex.plan_extract(str(p), None)
    with pytest.raises(ex.ExtractError, match="line break"):
        ex.extract_one(str(p), str(tmp_path / "out"), et=None)
    assert not (tmp_path / "out").exists()


# ── 6. overwrite verifies before replacing; temp names never derive from the photo ──

class XmpET(StubExif):
    """exiftool stand-in for the XMP path: reads nothing, tagsfromfile does nothing."""
    def execute(self, *a, **k):
        return ""


def test_overwrite_delete_mode_keeps_original_when_verification_fails(tmp_path, monkeypatch):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    monkeypatch.setattr("Muxer.verify_muxed_output", lambda *a, **k: "boom")
    m = Muxer(str(img), str(vid), StubExif("jpg"), overwrite=True, no_xmp=True, video_removal_mode="delete",
              video_removal_root=str(tmp_path))
    with pytest.raises(MuxerInputError, match="Original kept"):
        m.mux()
    assert img.read_bytes() == JPEG                       # never replaced
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "a.mp4"]     # temp file removed too


@pytest.mark.parametrize("overwrite", [True, False])
def test_user_sidecars_are_never_touched(tmp_path, overwrite):
    img, vid = tmp_path / "IMG.jpg", tmp_path / "IMG.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    sidecars = {"IMG.jpg.XMP": b"darktable sidecar", "IMG.jpg.xmp": b"digikam sidecar",
                "IMG.LIVE.jpg.XMP": b"sidecar 2", "IMG.XMP.jpg": b"user file", "IMG.LIVE.XMP.jpg": b"user 2"}
    for name, data in sidecars.items():
        (tmp_path / name).write_bytes(data)
    m = Muxer(str(img), str(vid), XmpET("jpg"), overwrite=overwrite, video_removal_mode="delete",
              video_removal_root=str(tmp_path))
    m.mux()
    for name, data in sidecars.items():
        assert (tmp_path / name).read_bytes() == data, name
    assert len(Path(m.output_fpath).read_bytes()) > len(JPEG)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".mp2")]
    assert leftovers == []


def test_no_overwrite_publish_never_replaces_a_file_that_appeared(tmp_path, monkeypatch):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif("jpg"), no_xmp=True)
    real = Muxer.check_output_collision

    def appears(self):
        real(self)
        Path(self.output_fpath).write_bytes(b"CREATED MEANWHILE")      # lost the race after the check
    monkeypatch.setattr(Muxer, "check_output_collision", appears)
    with pytest.raises(OutputConflictError):
        m.mux()
    assert (tmp_path / "a.LIVE.jpg").read_bytes() == b"CREATED MEANWHILE"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".mp2")]


# ── 8. Copy Unmuxed never overwrites a different file ─────────────────────────

def test_copy_unmuxed_uses_unique_name_for_a_different_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "doc.txt").write_bytes(b"NEW CONTENT")
    (dst / "doc.txt").write_bytes(b"OLD CONTENT")
    final = mp.copy_unmuxed_file(src / "doc.txt", dst / "doc.txt")
    assert (dst / "doc.txt").read_bytes() == b"OLD CONTENT"
    assert final == dst / "doc (2).txt" and final.read_bytes() == b"NEW CONTENT"
    # a second run finds its earlier copy and does nothing
    assert mp.copy_unmuxed_file(src / "doc.txt", dst / "doc.txt") is None
    assert sorted(p.name for p in dst.iterdir()) == ["doc (2).txt", "doc.txt"]


def test_copy_unmuxed_compares_content_not_size_and_mtime(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.bin").write_bytes(b"AAAA")
    (dst / "a.bin").write_bytes(b"BBBB")          # same size
    os.utime(dst / "a.bin", (os.path.getatime(src / "a.bin"), os.path.getmtime(src / "a.bin")))   # same mtime
    mp.copy_unmuxed_file(src / "a.bin", dst / "a.bin")
    assert (dst / "a.bin").read_bytes() == b"BBBB" and (dst / "a (2).bin").read_bytes() == b"AAAA"


# ── 9. bin estimate follows the real plan (EXIF matches too) ───────────────────

def test_estimate_counts_planned_pairs_not_same_name_files(tmp_path):
    (tmp_path / "IMG_1.jpg").write_bytes(b"x" * 10)
    (tmp_path / "IMG_1_video.mov").write_bytes(b"y" * 100)       # paired by content id, different name
    (tmp_path / "other.jpg").write_bytes(b"z" * 5)
    (tmp_path / "other.mov").write_bytes(b"w" * 7)               # same name but not part of the plan

    class Args:
        input_directory, overwrite, delete_video = str(tmp_path), True, True
    total, items = mp.estimate_bin_load(Args, {"IMG_1.jpg": "IMG_1_video.mov"}, tmp_path)
    assert (total, items) == (110, 2)


def test_cli_exif_matched_run_is_guarded_by_bin_guard(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for i in range(3):
        (root / f"p{i}.jpg").write_bytes(JPEG)
        (root / f"clip{i}.mov").write_bytes(VIDEO)               # names differ: only EXIF can pair them
    FakeET.ids = {f"p{i}.jpg": f"ID{i}" for i in range(3)} | {f"clip{i}.mov": f"ID{i}" for i in range(3)}
    monkeypatch.setattr(vr, "read_bin_settings", lambda p: {"cap": 40 * 1024 ** 3, "nuke": False, "source": "registry"})
    monkeypatch.setattr(vr, "read_bin_usage", lambda p: 0)
    monkeypatch.setenv("L2M_BIN_MAX_FILES", "2")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    try:
        code, out, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--exif-match", "--delete-video",
                               "--video-removal-mode", "trash")
    finally:
        FakeET.ids = {}
    assert code == 3 and vr.BIN_REFUSAL in out
    assert all((root / f"clip{i}.mov").exists() for i in range(3))


# ── 11. a shared video is kept until every photo that uses it is converted ────

class FakeMuxer:
    calls: list = []

    def __init__(self, image_fpath, video_fpath, delete_video=False, **kw):
        self.img, self.delete = image_fpath, delete_video
        self.output_fpath = image_fpath

    def mux(self):
        FakeMuxer.calls.append((Path(self.img).name, self.delete))


@pytest.mark.parametrize("exif", [False, True])
def test_shared_video_kept_when_only_one_of_its_photos_is_selected(tmp_path, monkeypatch, capsys, exif):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.HEIC", "a.JPEG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    (root / "a.MOV").write_bytes(b"vid")
    only = tmp_path / "only.json"
    only.write_text(json.dumps(["a.HEIC"]))
    FakeET.ids = {"a.HEIC": "ID-A", "a.JPEG": "ID-A", "a.MOV": "ID-A"}
    FakeMuxer.calls = []
    monkeypatch.setattr(mp, "Muxer", FakeMuxer)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        run_cli(monkeypatch, capsys, "--input-directory", str(root), "--recursive", "--delete-video",
                "--only-file", str(only), "--video-removal-mode", "move", *(["--exif-match"] if exif else []))
    finally:
        FakeET.ids = {}
    assert FakeMuxer.calls == [("a.HEIC", False)]          # a.JPEG still needs the video


# ── 12. a missing output folder is never created under a missing mount ────────

def test_cli_does_not_create_output_folder_parents(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    (root / "a.mov").write_bytes(VIDEO)
    out = tmp_path / "mnt" / "nas" / "photos"                    # "mnt/nas" is not mounted
    code, text, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--output-directory", str(out))
    assert code == 1 and "parent folder does not exist" in text
    assert not (tmp_path / "mnt").exists()


def test_cli_creates_only_the_last_output_folder(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    (root / "a.mov").write_bytes(VIDEO)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mp, "Muxer", FakeMuxer)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    code, _, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--output-directory", str(tmp_path / "newout"))
    assert code == 0 and (tmp_path / "newout").is_dir()


def test_cli_dry_run_creates_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    run_cli(monkeypatch, capsys, "--input-directory", str(root), "--dry-run", "--output-directory", str(tmp_path / "newout"))
    assert not (tmp_path / "newout").exists()


# ── 7 (CLI side). an empty input folder is not "the current folder" ────────────

def test_cli_refuses_empty_input_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.jpg").write_bytes(JPEG)
    (tmp_path / "a.mov").write_bytes(VIDEO)
    code, out, _ = run_cli(monkeypatch, capsys, "--input-directory", "")
    assert code == 1 and "empty" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "a.mov"]


# ── round 2 ────────────────────────────────────────────────────────────────────

def test_protect_path_keeps_verified_merged_photo_if_publish_fails_after_original_set_aside(tmp_path, monkeypatch, caplog):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)

    def disk_full(*a, **k):
        raise OSError("No space left on device")
    monkeypatch.setattr("Muxer.publish_no_overwrite", disk_full)
    m = Muxer(str(img), str(vid), StubExif("jpg"), overwrite=True, no_xmp=True, video_removal_mode="move",
              video_removal_root=str(tmp_path))
    with caplog.at_level("ERROR"), pytest.raises(MuxerInputError, match="verified merged photo is kept at"):
        m.mux()
    assert (tmp_path / "_originals" / "a.jpg").read_bytes() == JPEG            # the original, set aside
    kept = [p for p in tmp_path.iterdir() if p.name.startswith(".mp2tmp.")]
    assert len(kept) == 1 and kept[0].read_bytes().startswith(JPEG) and len(kept[0].read_bytes()) > len(JPEG)
    assert any(str(kept[0]) in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def test_rerun_skips_existing_output_and_blocks_video(tmp_path, monkeypatch, capsys):
    root, out = tmp_path / "in", tmp_path / "out"
    root.mkdir()
    out.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    (root / "a.mp4").write_bytes(MP4)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setenv("HOME", str(tmp_path))

    class ET(XmpET):
        def __init__(self, *a, **k):
            super().__init__("jpg")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    args = ("--input-directory", str(root), "--output-directory", str(out), "--delete-video",
            "--video-removal-mode", "move")
    code, _, rows = run_cli(monkeypatch, capsys, *args, et=ET)
    assert rows["a.jpg"]["status"] == "converted"
    (out / "a.jpg").write_bytes((out / "a.jpg").read_bytes() + b"EDITED")     # user edits the result
    (root / "a.mp4").write_bytes(MP4)                                           # video back for the rerun
    edited = (out / "a.jpg").read_bytes()
    code, text, rows = run_cli(monkeypatch, capsys, *args, et=ET)
    assert rows["a.jpg"]["status"] == "skipped" and "already converted" in rows["a.jpg"]["reason"]
    assert (out / "a.jpg").read_bytes() == edited and (root / "a.mp4").exists()   # not replaced, video kept


def test_output_may_never_be_the_input_video_or_image(tmp_path):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    assert "source video" in utils.output_conflict(str(vid), str(img), str(vid), rerun_ok=True)
    assert "source video" in utils.output_conflict(str(vid), str(img), str(vid), overwrite=True)
    m = Muxer(str(img), str(vid), StubExif("jpg"), output_fpath=str(vid), no_xmp=True)
    with pytest.raises(OutputConflictError):
        m.mux()
    assert vid.read_bytes() == MP4


def test_cli_refuses_output_inside_input_when_recursive(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    (root / "out").mkdir(parents=True)
    (root / "a.jpg").write_bytes(JPEG)
    code, text, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--recursive",
                            "--output-directory", str(root / "out"))
    assert code == 1 and "inside the input directory" in text
    code, text, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--recursive",
                            "--output-directory", str(root / "newout"))
    assert code == 1 and not (root / "newout").exists()


def test_cli_refuses_removal_folder_equal_to_input(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    (root / "a.mov").write_bytes(VIDEO)
    for folder in (str(root), "."):
        code, text, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--delete-video",
                                "--video-removal-mode", "move", "--video-removal-folder", folder)
        assert code == 1 and "cannot be the input folder" in text
    assert sorted(p.name for p in root.iterdir()) == ["a.jpg", "a.mov"]


@pytest.mark.parametrize("exif", [False, True])
def test_failed_photo_blocks_shared_video_removal(tmp_path, monkeypatch, capsys, exif):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.HEIC", "a.JPEG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    (root / "a.MOV").write_bytes(b"vid")
    FakeET.ids = {"a.HEIC": "ID-A", "a.JPEG": "ID-A", "a.MOV": "ID-A"}
    calls = []

    class M:
        def __init__(self, image_fpath, video_fpath, delete_video=False, **kw):
            self.img, self.delete, self.output_fpath = image_fpath, delete_video, image_fpath

        def mux(self):
            first = not calls
            calls.append((Path(self.img).name, self.delete))
            if first:                                  # the first photo fails, the last one succeeds
                raise MuxerInputError("exiftool cannot read the photo")
    monkeypatch.setattr(mp, "Muxer", M)
    monkeypatch.setattr(mp, "check_motion_photo", lambda p, et: (False, ""))
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        run_cli(monkeypatch, capsys, "--input-directory", str(root), "--delete-video", "--video-removal-mode",
                "move", *(["--exif-match"] if exif else []))
    finally:
        FakeET.ids = {}
    assert len(calls) == 2 and calls[1][1] is False         # the last photo no longer removes the video


# ── round 3 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exif", [False, True])
def test_unreadable_photo_blocks_removal_of_its_video(tmp_path, monkeypatch, capsys, exif):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.HEIC", "a.JPEG"):
        (root / n).write_bytes(JPEG + b"\x00" * 50)
    (root / "a.MOV").write_bytes(b"vid")
    FakeET.ids = {"a.HEIC": "ID-A", "a.JPEG": "ID-A", "a.MOV": "ID-A"}
    calls = []

    class M:
        def __init__(self, image_fpath, video_fpath, delete_video=False, **kw):
            self.img, self.delete, self.output_fpath = image_fpath, delete_video, image_fpath

        def mux(self):
            calls.append((Path(self.img).name, self.delete))
    monkeypatch.setattr(mp, "Muxer", M)
    monkeypatch.setattr(mp, "check_motion_photo",
                        lambda p, et: (False, "cannot read") if str(p).endswith("HEIC") else (False, ""))
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        run_cli(monkeypatch, capsys, "--input-directory", str(root), "--delete-video", "--video-removal-mode",
                "move", *(["--exif-match"] if exif else []))
    finally:
        FakeET.ids = {}
    assert calls == [("a.JPEG", False)]            # converted, but the video stays for the unreadable HEIC


def _protect_muxer(tmp_path):
    img, vid = tmp_path / "a.jpg", tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    return img, Muxer(str(img), str(vid), StubExif("jpg"), overwrite=True, no_xmp=True, video_removal_mode="move",
                      video_removal_root=str(tmp_path))


def temps(tmp_path):
    return [p for p in tmp_path.iterdir() if p.name.startswith(".mp2tmp.")]


def test_cancel_during_dispose_never_deletes_the_merged_file(tmp_path, monkeypatch):
    img, m = _protect_muxer(tmp_path)

    def cancelled_midway(path, **k):
        os.makedirs(tmp_path / "_originals")
        os.rename(path, tmp_path / "_originals" / "a.jpg")      # the original already moved...
        raise KeyboardInterrupt                                  # ...when Cancel arrives
    monkeypatch.setattr("Muxer.dispose_file", cancelled_midway)
    with pytest.raises(KeyboardInterrupt):
        m.mux()
    assert (tmp_path / "_originals" / "a.jpg").read_bytes() == JPEG
    assert len(temps(tmp_path)) == 1                              # merged photo kept


def test_cancel_before_original_moved_removes_only_the_temp(tmp_path, monkeypatch):
    img, m = _protect_muxer(tmp_path)

    def cancelled_early(path, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr("Muxer.dispose_file", cancelled_early)
    with pytest.raises(KeyboardInterrupt):
        m.mux()
    assert img.read_bytes() == JPEG and temps(tmp_path) == []


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_cancel_during_publish_propagates_and_keeps_merged_file(tmp_path, monkeypatch, caplog, exc):
    img, m = _protect_muxer(tmp_path)
    real = utils.publish_no_overwrite
    n = {"calls": 0}

    def cancel_first(*a, **k):
        n["calls"] += 1
        if n["calls"] == 1:
            raise exc()
        return real(*a, **k)
    monkeypatch.setattr("Muxer.publish_no_overwrite", cancel_first)
    with caplog.at_level("ERROR"), pytest.raises(exc):
        m.mux()
    merged = tmp_path / "a (merged).jpg"
    assert merged.exists() and len(merged.read_bytes()) > len(JPEG)
    assert (tmp_path / "_originals" / "a.jpg").read_bytes() == JPEG
    assert any(str(merged) in r.getMessage() for r in caplog.records)


def test_failed_publish_gives_merged_photo_a_visible_name_in_the_error(tmp_path, monkeypatch):
    img, m = _protect_muxer(tmp_path)
    real = utils.publish_no_overwrite
    n = {"calls": 0}

    def fail_first(*a, **k):
        n["calls"] += 1
        if n["calls"] == 1:
            raise OSError("No space left on device")
        return real(*a, **k)
    monkeypatch.setattr("Muxer.publish_no_overwrite", fail_first)
    (tmp_path / "a (merged).jpg").write_bytes(b"USER FILE")      # never replaced: next unique name
    with pytest.raises(MuxerInputError, match=r"a \(merged\) \(2\)\.jpg"):
        m.mux()
    assert (tmp_path / "a (merged).jpg").read_bytes() == b"USER FILE"
    assert (tmp_path / "a (merged) (2).jpg").read_bytes().startswith(JPEG) and temps(tmp_path) == []


def test_folder_comparison_survives_alias_spellings(tmp_path):
    root = tmp_path / "in"
    (root / "out").mkdir(parents=True)
    alias = tmp_path / "ALIAS"                    # stands in for a differently-cased path on macOS
    alias.symlink_to(root, target_is_directory=True)
    assert vr.same_folder(alias, root) and vr.same_folder(root, root / ".")
    assert vr.inside_folder(alias / "out", root) and vr.inside_folder(root / "new" / "deeper", alias)
    assert not vr.inside_folder(tmp_path / "other", root)
    assert vr.same_folder(tmp_path / "missing", tmp_path / "missing")


def test_cli_catches_aliased_output_and_removal_folders(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    (root / "out").mkdir(parents=True)
    (root / "a.jpg").write_bytes(JPEG)
    alias = tmp_path / "ALIAS"
    alias.symlink_to(root, target_is_directory=True)
    code, text, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--recursive",
                            "--output-directory", str(alias / "out"))
    assert code == 1 and "inside the input directory" in text
    code, text, _ = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--delete-video",
                            "--video-removal-mode", "move", "--video-removal-folder", str(alias))
    assert code == 1 and "cannot be the input folder" in text


def test_scan_skips_only_the_apps_own_temp_prefixes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.jpg", ".mp2tmp.x.jpg", ".mp2xmp.y.jpg", ".mp2mv.z.jpg", ".mp2holiday.jpg"):
        (root / n).write_bytes(JPEG)
    code, text, rows = run_cli(monkeypatch, capsys, "--input-directory", str(root), "--dry-run")
    assert sorted(rows) == [".mp2holiday.jpg", "a.jpg"]
    assert vr.is_own_temp(".mp2tmp.q") and not vr.is_own_temp(".mp2holiday.jpg")
