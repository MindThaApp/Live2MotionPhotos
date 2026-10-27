import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")
pytest.importorskip("httpx")

import send2trash  # noqa: E402

import app as appmod  # noqa: E402
import video_removal as vr  # noqa: E402
from Muxer import Muxer  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


class StubExif:
    def __init__(self, image_ext="jpg"):
        self.image_ext = image_ext

    def get_metadata(self, files):
        return [{"File:FileTypeExtension": self.image_ext}, {"File:FileTypeExtension": "mp4"}]


def mux(tmp_path, mode, name="a.jpg", ext="jpg", **kw):
    img = tmp_path / name
    vid = tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif(ext), overwrite=True, no_xmp=True,
              video_removal_mode=mode, video_removal_root=str(tmp_path), **kw)
    m.mux()
    return img, vid, m


def test_overwrite_trash_sends_original_to_bin_before_replacing(tmp_path, monkeypatch):
    trashed = []

    def fake(p):
        assert Path(p).read_bytes() == JPEG       # still the original at this point
        trashed.append(p)
        Path(p).unlink()
    monkeypatch.setattr(send2trash, "send2trash", fake)
    img, _, m = mux(tmp_path, "trash")
    assert trashed == [str(img.resolve())]
    assert img.read_bytes().startswith(JPEG) and len(img.read_bytes()) > len(JPEG)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".mp2")]


def test_overwrite_trash_failure_falls_back_to_move(tmp_path, monkeypatch):
    monkeypatch.setattr(send2trash, "send2trash", lambda p: (_ for _ in ()).throw(OSError("no bin")))
    img, _, _ = mux(tmp_path, "trash")
    assert (tmp_path / "_originals" / "a.jpg").read_bytes() == JPEG
    assert len(img.read_bytes()) > len(JPEG)


def test_overwrite_move_uses_same_folder_for_photo_and_video(tmp_path):
    img, vid, _ = mux(tmp_path, "move", delete_video=True, video_removal_folder="old")
    assert (tmp_path / "old" / "a.jpg").read_bytes() == JPEG
    assert (tmp_path / "old" / "a.mp4").read_bytes() == MP4
    assert not vid.exists() and len(img.read_bytes()) > len(JPEG)


def test_overwrite_delete_keeps_old_behaviour(tmp_path):
    img, vid, _ = mux(tmp_path, "delete")
    assert len(img.read_bytes()) > len(JPEG) and vid.exists()
    assert not (tmp_path / "_originals").exists()


def test_overwrite_aborts_and_keeps_original_if_set_aside_fails(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr("Muxer.dispose_file", boom)
    img = tmp_path / "a.jpg"
    vid = tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    m = Muxer(str(img), str(vid), StubExif(), overwrite=True, no_xmp=True, video_removal_mode="move")
    with pytest.raises(OSError):
        m.mux()
    assert img.read_bytes() == JPEG
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["a.jpg", "a.mp4"]


def test_extension_mismatch_original_goes_through_move_not_remove(tmp_path):
    # a .jpg that is really a HEIC: output gets the new extension, original is set aside
    img, _, m = mux(tmp_path, "move", name="a.jpg", ext="heic")
    out = Path(m.output_fpath)
    assert out.suffix == ".heic" and out.exists()
    assert (tmp_path / "_originals" / "a.jpg").read_bytes() == JPEG
    assert not img.exists()


def test_dispose_file_rejects_outside_root(tmp_path):
    root = tmp_path / "in"
    root.mkdir()
    other = tmp_path / "x.jpg"
    other.write_bytes(b"1")
    with pytest.raises(ValueError):
        vr.dispose_file(other, "move", root)
    assert other.exists()


def test_legacy_folder_still_skipped():
    folders = [p.name for p in vr.skip_folders("/in")]
    assert folders == ["_originals", "_deleted_videos"]


# ── config ──────────────────────────────────────────────────────────────────

def test_validate_rejects_overwrite_with_output_directory():
    with pytest.raises(ValueError, match="Overwrite"):
        appmod.validate_config({"overwrite": True, "output_directory": "/x"})
    assert appmod.validate_config({"overwrite": True, "output_directory": ""})["overwrite"] is True
    assert appmod.validate_config({"overwrite": False, "output_directory": "/x"})


def test_save_rejects_merged_overwrite_with_output(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"output_directory": str(tmp_path)}))
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    r = c.post("/api/config", json={"overwrite": True})
    assert r.status_code == 400


def test_old_config_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps(
        {"input_directory": str(tmp_path), "delete_video": True, "video_removal_mode": "bogus"}))
    cfg = appmod.load_config()
    assert cfg["video_removal_mode"] in vr.MODES and cfg["delete_video"] is True
    (tmp_path / "config.json").write_text(json.dumps({"input_directory": str(tmp_path)}))
    assert appmod.load_config()["video_removal_mode"] in vr.MODES


def test_build_cmd_passes_mode_when_overwrite_only():
    cmd = appmod.build_cmd({**appmod.DEFAULT_CONFIG, "overwrite": True, "video_removal_mode": "move",
                            "video_removal_folder": "bin"})
    assert cmd[cmd.index("--video-removal-mode") + 1] == "move"
    assert cmd[cmd.index("--video-removal-folder") + 1] == "bin"
    assert "--delete-video" not in cmd


# ── preview -> run selected ────────────────────────────────────────────────

@pytest.fixture
def preview_env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "IN_DOCKER", False)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(tmp_path))
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    root = tmp_path / "in"
    (root / "sub").mkdir(parents=True)
    for n in ("a.jpg", "sub/b.jpg"):
        (root / n).write_bytes(JPEG)
    cfg = {**appmod.DEFAULT_CONFIG, "input_directory": str(root)}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    prev = {"id": "p1", "fingerprint": appmod.preview_fingerprint(cfg), "names": {"a.jpg", str(Path("sub/b.jpg"))}}
    monkeypatch.setattr(appmod, "last_preview", prev)
    return root, cfg, prev


def test_selected_paths_validated(preview_env):
    root, cfg, prev = preview_env
    assert appmod.validate_selected("p1", ["a.jpg"], cfg, prev) == ["a.jpg"]
    for bad in (["../evil.jpg"], ["/etc/passwd"], ["c.jpg"], []):
        with pytest.raises(ValueError):
            appmod.validate_selected("p1", bad, cfg, prev)
    with pytest.raises(ValueError, match="Preview"):
        appmod.validate_selected("other", ["a.jpg"], cfg, prev)
    (root / "a.jpg").unlink()           # gone files are skipped and reported, not an error
    skipped = []
    assert appmod.validate_selected("p1", ["a.jpg", str(Path("sub/b.jpg"))], cfg, prev, skipped) == [str(Path("sub/b.jpg"))]
    assert skipped == [{"file": "a.jpg", "reason": "file no longer exists"}]


def test_stale_preview_detected(preview_env):
    _, cfg, prev = preview_env
    assert appmod.preview_status()["stale"] is False
    for k, v in (("overwrite", True), ("video_removal_mode", "delete"), ("recursive", False)):
        changed = {**cfg, k: v}
        appmod.CONFIG_FILE.write_text(json.dumps(changed))
        assert appmod.preview_status()["stale"] is True
        with pytest.raises(ValueError, match="changed"):
            appmod.validate_selected("p1", ["a.jpg"], appmod.load_config(), prev)
    appmod.CONFIG_FILE.write_text(json.dumps({**cfg, "schedule_cron": "5 5 * * *"}))   # irrelevant setting
    assert appmod.preview_status()["stale"] is False


def test_run_selected_endpoint_runs_only_selected(preview_env, monkeypatch):
    from fastapi.testclient import TestClient
    seen = []
    monkeypatch.setattr(appmod, "do_run", lambda *a: seen.append(a))
    monkeypatch.setattr(appmod.run_state, "running", False)
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    r = c.post("/api/run-selected", json={"preview_id": "p1", "files": ["a.jpg"]})
    assert r.status_code == 200 and r.json()["count"] == 1
    import time; time.sleep(0.2)
    assert seen[0][1] is False and seen[0][3] == ["a.jpg"]
    assert c.post("/api/run-selected", json={"preview_id": "p1", "files": ["../x.jpg"]}).status_code == 400
    assert c.post("/api/run-selected", json={"preview_id": "zzz", "files": ["a.jpg"]}).json()["stale"] is True


def test_build_cmd_only_file():
    cmd = appmod.build_cmd({**appmod.DEFAULT_CONFIG, "only_file": "/tmp/x.json"})
    assert cmd[cmd.index("--only-file") + 1] == "/tmp/x.json"


def test_cli_only_file_filters_images(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("exiftool"):
        pytest.skip("exiftool binary not installed")
    root = tmp_path / "in"
    root.mkdir()
    for n in ("a.jpg", "b.jpg"):
        (root / n).write_bytes(JPEG)
        (root / n.replace("jpg", "mov")).write_bytes(MP4)
    only = tmp_path / "only.json"
    only.write_text(json.dumps(["a.jpg"]))
    r = subprocess.run([sys.executable, str(ROOT / "MotionPhoto2" / "motionphoto2.py"), "--input-directory",
                        str(root), "--dry-run", "--only-file", str(only)],
                       capture_output=True, text=True, cwd=tmp_path)
    assert '"file": "a.jpg"' in r.stdout and '"file": "b.jpg"' not in r.stdout, r.stdout + r.stderr


def _rows(*names):
    return [{"file": n, "status": "planned", "reason": "", "output": ""} for n in names]


def test_preview_persists_and_reloads(preview_env, monkeypatch):
    _, cfg, _ = preview_env
    appmod.set_preview(appmod.preview_fingerprint(cfg), _rows("a.jpg"))
    saved = json.loads(appmod.preview_file().read_text())
    assert saved["rows"][0]["file"] == "a.jpg" and not appmod.preview_file().with_name("preview.json.tmp").exists()
    pid = appmod.last_preview["id"]
    appmod.last_preview.clear()                      # simulate an app restart
    st = appmod.preview_status()
    assert st["id"] == pid and st["count"] == 1 and st["stale"] is False and st["created"]
    assert st["rows"][0]["file"] == "a.jpg"


def test_corrupt_preview_file_ignored(preview_env):
    appmod.last_preview.clear()
    appmod.preview_file().write_text("{nope")
    assert appmod.preview_status()["id"] is None


def test_selected_skips_gone_and_done(preview_env):
    root, cfg, _ = preview_env
    appmod.set_preview(appmod.preview_fingerprint(cfg), _rows("a.jpg", str(Path("sub/b.jpg"))))
    prev = appmod.last_preview
    prev["done"].add("a.jpg")
    (root / "sub" / "b.jpg").unlink()
    skipped = []
    assert appmod.validate_selected(prev["id"], ["a.jpg", str(Path("sub/b.jpg"))], cfg, prev, skipped) == []
    assert {s["reason"] for s in skipped} == {"already converted", "file no longer exists"}


def test_run_selected_all_skipped_does_not_run(preview_env, monkeypatch):
    from fastapi.testclient import TestClient
    root, cfg, _ = preview_env
    appmod.set_preview(appmod.preview_fingerprint(cfg), _rows("a.jpg"))
    (root / "a.jpg").unlink()
    seen = []
    monkeypatch.setattr(appmod, "do_run", lambda *a: seen.append(a))
    monkeypatch.setattr(appmod.run_state, "running", False)
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    r = c.post("/api/run-selected", json={"preview_id": appmod.last_preview["id"], "files": ["a.jpg"]})
    assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["skipped"][0]["file"] == "a.jpg"
    import time; time.sleep(0.2)
    assert seen == []


def test_stale_preview_returns_409_out_of_date(preview_env):
    from fastapi.testclient import TestClient
    _, cfg, _ = preview_env
    appmod.set_preview(appmod.preview_fingerprint(cfg), _rows("a.jpg"))
    pid = appmod.last_preview["id"]
    appmod.CONFIG_FILE.write_text(json.dumps({**cfg, "overwrite": True}))
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    r = c.post("/api/run-selected", json={"preview_id": pid, "files": ["a.jpg"]})
    assert r.status_code == 409 and "Preview is out of date" in r.json()["error"]


def test_mark_done_then_clear(preview_env):
    _, cfg, _ = preview_env
    appmod.set_preview(appmod.preview_fingerprint(cfg), _rows("a.jpg", "b.jpg"))
    pid = appmod.last_preview["id"]
    appmod.mark_preview_done(pid, ["a.jpg"])
    st = appmod.preview_status()
    assert st["count"] == 1 and [r["done"] for r in st["rows"]] == [True, False]
    appmod.last_preview.clear()                      # done flags survive a restart too
    assert appmod.preview_status()["count"] == 1
    appmod.mark_preview_done(pid, ["b.jpg"])
    assert appmod.preview_status()["id"] is None and not appmod.preview_file().exists()


def test_ui_has_no_dry_run_tickbox_and_run_sends_false():
    html = (Path(appmod.__file__).parent / "index.html").read_text(encoding="utf-8")
    assert 'id="dry_run"' not in html and "gc('dry_run')" not in html
    assert "'/api/run?dry_run=' + (dryRun === true)" in html
