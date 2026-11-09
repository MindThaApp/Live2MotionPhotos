"""Regression tests for the 2026-11-09 review findings in video_removal.py
(trash safety, verified move, scan skips). Temp dirs only."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

import video_removal as vr  # noqa: E402

GB = 1024 ** 3


# ── 2. trash is refused where it would delete permanently ─────────────────────

def test_trash_refused_in_docker_and_nothing_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_DOCKER", "1")
    f = tmp_path / "a.mov"
    f.write_bytes(b"x")
    with pytest.raises(vr.TrashUnavailable, match="Move to folder"):
        vr.dispose_file(f, "trash", root=tmp_path)
    assert f.read_bytes() == b"x" and not (tmp_path / "_originals").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".Trash")]


def _fake_ctypes(volume, kind):
    """ctypes stand-in: GetVolumePathNameW reports `volume`, GetDriveTypeW(volume) reports kind."""
    import types
    seen = {}

    def vol(path, buf, n):
        seen["path"] = path
        buf.value = volume
        return 1 if volume is not None else 0

    def drive(root):
        seen["root"] = root
        return kind
    return types.SimpleNamespace(
        create_unicode_buffer=lambda n: types.SimpleNamespace(value=""),
        windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(GetVolumePathNameW=vol, GetDriveTypeW=drive))), seen


def test_trash_refused_on_non_fixed_windows_drives(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "in_docker", lambda: False)
    monkeypatch.setattr(vr, "is_windows", lambda: True)
    for kind in (0, 1, 2, 4, 5, 6):                      # unknown, no root, removable, network, cd, ram
        fake, _ = _fake_ctypes("E:\\", kind)
        monkeypatch.setitem(sys.modules, "ctypes", fake)
        assert "Recycle Bin" in vr.trash_unavailable_reason("E:\\photos")
    fake, _ = _fake_ctypes("C:\\", vr.DRIVE_FIXED)
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    assert vr.trash_unavailable_reason("C:\\photos") == ""
    assert "UNC" in vr.trash_unavailable_reason("\\\\nas\\share\\photos")


def test_trash_checks_the_real_volume_of_a_folder_mounted_usb_stick(monkeypatch):
    monkeypatch.setattr(vr, "in_docker", lambda: False)
    monkeypatch.setattr(vr, "is_windows", lambda: True)
    # C:\mnt\usb is a removable volume mounted into a folder of the fixed C: drive
    fake, seen = _fake_ctypes("C:\\mnt\\usb\\", 2)
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    assert "Recycle Bin" in vr.trash_unavailable_reason("C:\\mnt\\usb\\DCIM")
    assert seen["root"] == "C:\\mnt\\usb\\"            # the drive type was asked for the real volume
    fake, _ = _fake_ctypes(None, vr.DRIVE_FIXED)       # GetVolumePathNameW fails: refuse
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    assert "could not be determined" in vr.trash_unavailable_reason("C:\\x")
    fake, _ = _fake_ctypes("\\\\?\\Volume{1}\\", vr.DRIVE_FIXED)
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    assert vr.trash_unavailable_reason("C:\\x")               # \\?\ style volume names are refused (conservative)


def test_bin_guard_refuses_unavailable_trash_and_unknown_windows_cap(monkeypatch):
    monkeypatch.setattr(vr, "trash_unavailable_reason", lambda p: "no bin here")
    rep = vr.bin_report(1, 1, ".")
    assert not rep["ok"] and "no bin here" in rep["reason"] and "Move to folder" in rep["reason"]
    monkeypatch.setattr(vr, "trash_unavailable_reason", lambda p: "")
    monkeypatch.setattr(vr, "is_windows", lambda: True)
    monkeypatch.setattr(vr, "read_bin_settings", lambda p: {"cap": None, "nuke": False, "source": "default"})
    monkeypatch.setattr(vr, "read_bin_usage", lambda p: None)
    monkeypatch.setattr(vr, "drive_size", lambda p: 1000 * GB)
    monkeypatch.delenv("L2M_BIN_CAP_MB", raising=False)
    rep = vr.bin_report(1, 1, ".")
    assert not rep["ok"] and rep["cap_source"] == "unknown"          # never assumes 5%


def test_scan_skips_trash_and_originals_folders():
    for name in (".Trash-1000", ".Trash", ".trash-0", "_originals", "_deleted_videos", "$RECYCLE.BIN", "_ORIGINALS"):
        assert vr.skip_dir_name(name), name
    for name in ("Trip", "originals", "Trash", "2024"):
        assert not vr.skip_dir_name(name), name


def test_recursive_scan_does_not_enter_trash_or_originals(tmp_path, monkeypatch, capsys):
    import json
    import motionphoto2 as mp
    from test_preview_filters import JPEG, VIDEO, FakeET
    root = tmp_path / "in"
    for d in ("keep", ".Trash-1000/files", "sub/_originals"):
        (root / d).mkdir(parents=True)
        (root / d / "a.jpg").write_bytes(JPEG)
        (root / d / "a.mov").write_bytes(VIDEO)
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--recursive", "--dry-run",
                                      "--video-removal-folder", str(tmp_path / "elsewhere")])
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        mp.main()
    except SystemExit:
        pass
    files = [json.loads(line[9:])["file"].replace("\\", "/") for line in capsys.readouterr().out.splitlines()
             if line.startswith("@@RESULT ")]
    assert files == ["keep/a.jpg"]


def test_cli_refuses_trash_mode_when_unavailable(tmp_path, monkeypatch, capsys):
    import motionphoto2 as mp
    from test_preview_filters import JPEG, VIDEO, FakeET
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG)
    (root / "a.mov").write_bytes(VIDEO)
    monkeypatch.setenv("LIVE2MOTION_DOCKER", "1")
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--delete-video",
                                      "--video-removal-mode", "trash"])
    with pytest.raises(SystemExit) as e:
        mp.main()
    out = capsys.readouterr().out
    assert e.value.code == 3 and "Move to folder" in out
    assert sorted(p.name for p in root.iterdir()) == ["a.jpg", "a.mov"]


# ── 10. move = verified copy / no-overwrite link, then remove the source ──────

def test_move_never_overwrites_and_keeps_both(tmp_path):
    root = tmp_path / "in"
    (root / "_originals").mkdir(parents=True)
    (root / "_originals" / "a.mov").write_bytes(b"EARLIER")
    (root / "a.mov").write_bytes(b"NEW")
    assert vr.dispose_file(root / "a.mov", "move", root=root) == "move"
    assert (root / "_originals" / "a.mov").read_bytes() == b"EARLIER"
    assert (root / "_originals" / "a (2).mov").read_bytes() == b"NEW"
    assert not (root / "a.mov").exists()


def test_cross_volume_move_verifies_before_removing_source(tmp_path, monkeypatch):
    src = tmp_path / "a.mov"
    src.write_bytes(os.urandom(300_000))
    data = src.read_bytes()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    monkeypatch.setattr(vr, "same_volume", lambda a, b: False)      # force the copy + verify path
    final = vr.safe_move(src, dest_dir / "a.mov")
    assert final.read_bytes() == data and not src.exists()
    assert [p.name for p in dest_dir.iterdir()] == ["a.mov"]          # no temp left behind


def test_cross_volume_move_keeps_source_if_copy_is_corrupt(tmp_path, monkeypatch):
    src = tmp_path / "a.mov"
    src.write_bytes(b"A" * 5000)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    monkeypatch.setattr(vr, "same_volume", lambda a, b: False)
    real_copy = vr.shutil.copyfileobj

    def bad_copy(inp, out, n=0):
        real_copy(inp, out, n)
        out.write(b"!")                                  # a corrupt / truncated-style copy
    monkeypatch.setattr(vr.shutil, "copyfileobj", bad_copy)
    with pytest.raises(OSError, match="does not match"):
        vr.safe_move(src, dest_dir / "a.mov")
    assert src.read_bytes() == b"A" * 5000
    assert list(dest_dir.iterdir()) == []                # no partial file, no temp


def test_move_never_replaces_a_destination_that_appears_late(tmp_path, monkeypatch):
    src = tmp_path / "a.mov"
    src.write_bytes(b"NEW")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    real_link = os.link
    state = {"n": 0}

    def racing_link(s, d, *a, **k):
        if state["n"] == 0:                               # somebody creates the target first
            state["n"] += 1
            Path(d).write_bytes(b"OTHER")
        return real_link(s, d, *a, **k)
    monkeypatch.setattr(vr.os, "link", racing_link)
    final = vr.safe_move(src, dest_dir / "a.mov")
    assert (dest_dir / "a.mov").read_bytes() == b"OTHER" and final.read_bytes() == b"NEW"


def test_move_without_hard_links_still_never_overwrites(tmp_path, monkeypatch):
    src = tmp_path / "a.mov"
    src.write_bytes(b"NEW")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "a.mov").write_bytes(b"OTHER")

    def no_links(*a, **k):
        raise PermissionError("FAT")
    monkeypatch.setattr(vr.os, "link", no_links)
    final = vr.safe_move(src, dest_dir / "a.mov")
    assert (dest_dir / "a.mov").read_bytes() == b"OTHER" and final.name == "a (2).mov"
    assert final.read_bytes() == b"NEW" and not src.exists()


def test_move_refuses_to_build_a_removal_folder_under_a_missing_parent(tmp_path):
    root = tmp_path / "in"
    root.mkdir()
    f = root / "a.mov"
    f.write_bytes(b"x")
    with pytest.raises(OSError, match="parent does not exist"):
        vr.dispose_file(f, "move", root=root, folder=str(tmp_path / "mnt" / "nas" / "orig"))
    assert f.exists() and not (tmp_path / "mnt").exists()


# ── round 2 ────────────────────────────────────────────────────────────────────

def test_same_volume_move_removes_new_link_if_source_cannot_be_unlinked(tmp_path, monkeypatch):
    src = tmp_path / "a.mov"
    src.write_bytes(b"DATA")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    real_unlink = os.unlink

    def locked(p, *a, **k):
        if Path(p) == src:
            raise PermissionError("locked")
        return real_unlink(p, *a, **k)
    monkeypatch.setattr(vr.os, "unlink", locked)
    with pytest.raises(vr.SourceNotRemoved):
        vr.safe_move(src, dest_dir / "a.mov")
    assert src.read_bytes() == b"DATA" and list(dest_dir.iterdir()) == []      # no link, no copy left behind


def test_scan_skips_the_apps_own_temp_files(tmp_path, monkeypatch, capsys):
    import json
    import motionphoto2 as mp
    from test_preview_filters import JPEG, VIDEO, FakeET
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.jpg", ".mp2xmp.abc.jpg", ".mp2tmp.x.jpg"):
        (root / n).write_bytes(JPEG)
    (root / "a.mov").write_bytes(VIDEO)
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--dry-run"])
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        mp.main()
    except SystemExit:
        pass
    files = [json.loads(line[9:])["file"] for line in capsys.readouterr().out.splitlines() if line.startswith("@@RESULT ")]
    assert files == ["a.jpg"] and vr.skip_dir_name(".mp2xmp.dir")
