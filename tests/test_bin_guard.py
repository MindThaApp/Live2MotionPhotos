"""Recycle Bin guard: big runs refuse "trash" (never a silent delete); cap and usage are mocked."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

import video_removal as vr  # noqa: E402

GB = 1024 ** 3


@pytest.fixture
def fake_bin(monkeypatch):
    state = {"cap": 40 * GB, "nuke": False, "usage": 0}
    monkeypatch.setattr(vr, "read_bin_settings",
                        lambda p: {"cap": state["cap"], "nuke": state["nuke"], "source": "registry"})
    monkeypatch.setattr(vr, "read_bin_usage", lambda p: state["usage"])
    monkeypatch.delenv("L2M_BIN_CAP_MB", raising=False)
    monkeypatch.delenv("L2M_BIN_MAX_FILES", raising=False)
    return state


def test_small_run_ok(fake_bin):
    rep = vr.bin_report(2 * GB, 20, ".")
    assert rep["ok"] and rep["reason"] == "" and rep["cap"] == 40 * GB


def test_over_half_of_cap_refused(fake_bin):
    rep = vr.bin_report(21 * GB, 20, ".")
    assert not rep["ok"] and rep["reason"].startswith(vr.BIN_REFUSAL)
    with pytest.raises(vr.BinTooBig):
        vr.check_bin(21 * GB, 20, ".")


def test_current_usage_counts(fake_bin):
    fake_bin["usage"] = 15 * GB
    assert vr.bin_report(4 * GB, 5, ".")["ok"]
    assert not vr.bin_report(6 * GB, 5, ".")["ok"]


def test_nuke_on_delete_refused(fake_bin):
    fake_bin["nuke"] = True
    rep = vr.bin_report(1, 1, ".")
    assert not rep["ok"] and "immediately" in rep["reason"]


def test_too_many_files_refused(fake_bin):
    assert vr.bin_report(1000, 500, ".")["ok"]
    assert not vr.bin_report(1000, 501, ".")["ok"]


def test_env_overrides(fake_bin, monkeypatch):
    monkeypatch.setenv("L2M_BIN_MAX_FILES", "3")
    assert not vr.bin_report(1, 4, ".")["ok"]
    monkeypatch.setenv("L2M_BIN_CAP_MB", "10")
    rep = vr.bin_report(6 * 1024 * 1024, 1, ".")
    assert rep["cap"] == 10 * 1024 * 1024 and not rep["ok"]


def test_default_cap_is_5_percent_of_drive(monkeypatch):
    monkeypatch.setattr(vr, "read_bin_settings", lambda p: {"cap": None, "nuke": False, "source": "default"})
    monkeypatch.setattr(vr, "read_bin_usage", lambda p: None)
    monkeypatch.setattr(vr, "drive_size", lambda p: 1000 * GB)
    rep = vr.bin_report(10 * GB, 2, ".")
    assert rep["cap"] == 50 * GB and rep["cap_source"] == "default" and rep["ok"]
    assert not vr.bin_report(30 * GB, 2, ".")["ok"]


def test_unknown_cap_refused(monkeypatch):
    monkeypatch.setattr(vr, "read_bin_settings", lambda p: {"cap": None, "nuke": False, "source": "default"})
    monkeypatch.setattr(vr, "read_bin_usage", lambda p: None)
    monkeypatch.setattr(vr, "drive_size", lambda p: 0)
    assert not vr.bin_report(1, 1, ".")["ok"]


def test_dispose_fallback_still_moves_never_deletes(tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_trash(name, *a, **k):
        if name == "send2trash":
            raise ImportError("no bin")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_trash)
    f = tmp_path / "a.mov"
    f.write_bytes(b"x")
    assert vr.dispose_file(f, "trash", root=tmp_path) == "move"
    assert (tmp_path / "_originals" / "a.mov").exists()


# ── CLI ────────────────────────────────────────────────────────────────────

pytest.importorskip("exiftool")
pytest.importorskip("lxml")
import motionphoto2 as mp  # noqa: E402
from test_preview_filters import JPEG, VIDEO, FakeET  # noqa: E402


def make_dir(root, n=3):
    root.mkdir()
    for i in range(n):
        (root / f"p{i}.jpg").write_bytes(JPEG)
        (root / f"p{i}.mov").write_bytes(VIDEO)


def run_cli(root, monkeypatch, capsys, *extra):
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), *extra])
    monkeypatch.setenv("HOME", str(root.parent))
    code = 0
    try:
        mp.main()
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def test_cli_refuses_big_trash_run(tmp_path, monkeypatch, capsys, fake_bin):
    root = tmp_path / "in"
    make_dir(root)
    monkeypatch.setenv("L2M_BIN_MAX_FILES", "2")
    code, out = run_cli(root, monkeypatch, capsys, "--delete-video", "--video-removal-mode", "trash")
    assert code == 3 and vr.BIN_REFUSAL in out
    assert all((root / f"p{i}.mov").exists() for i in range(3))     # nothing touched, nothing deleted
    assert not (root / "_originals").exists()


def test_cli_move_mode_not_guarded(tmp_path, monkeypatch, capsys, fake_bin):
    root = tmp_path / "in"
    make_dir(root)
    monkeypatch.setenv("L2M_BIN_MAX_FILES", "0")
    code, out = run_cli(root, monkeypatch, capsys, "--delete-video", "--video-removal-mode", "move")
    assert vr.BIN_REFUSAL not in out and code != 3


def test_cli_dry_run_not_guarded_and_reports_bytes(tmp_path, monkeypatch, capsys, fake_bin):
    root = tmp_path / "in"
    make_dir(root, 1)
    monkeypatch.setenv("L2M_BIN_MAX_FILES", "0")
    code, out = run_cli(root, monkeypatch, capsys, "--dry-run", "--delete-video", "--overwrite",
                        "--video-removal-mode", "trash")
    rows = [json.loads(line[9:]) for line in out.splitlines() if line.startswith("@@RESULT ")]
    assert code != 3 and rows[0]["bytes"] == len(JPEG) + len(VIDEO)


# ── app ────────────────────────────────────────────────────────────────────

pytest.importorskip("httpx")
import app as appmod  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch, fake_bin):
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "IN_DOCKER", False)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(tmp_path))
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    root = tmp_path / "in"
    make_dir(root)
    # p0 and p1 are Motion Photos with a leftover MOV; p2 is a plain pair
    from test_preview_filters import motion_photo_bytes
    for i in (0, 1):
        (root / f"p{i}.jpg").write_bytes(motion_photo_bytes())
    cfg = {**appmod.DEFAULT_CONFIG, "input_directory": str(root), "video_removal_mode": "trash",
           "delete_video": True}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(appmod, "last_preview", {})
    started = []
    monkeypatch.setattr(appmod, "do_run", lambda *a, **k: started.append(a))
    rows = [{"file": "p2.jpg", "status": "planned", "kind": "ready", "bytes": 3 * GB},
            {"file": "p0.jpg", "status": "skipped", "kind": "motion_leftover", "video": "p0.mov"},
            {"file": "p1.jpg", "status": "skipped", "kind": "motion_leftover", "video": "p1.mov"}]
    appmod.set_preview(appmod.preview_fingerprint(cfg), rows)
    from fastapi.testclient import TestClient
    return root, TestClient(appmod.app, base_url="http://localhost:7000"), fake_bin, started


def test_bin_check_endpoint_estimate(env):
    _, c, _, _ = env
    r = c.post("/api/bin-check", json={"files": ["p2.jpg"], "action": "convert"}).json()
    assert r["applies"] and r["ok"] and r["estimate"] == 3 * GB and r["cap"] == 40 * GB and r["files"] == 1


def test_bin_check_not_applicable_in_move_mode(env):
    _, c, _, _ = env
    cfg = appmod.load_config()
    appmod.save_config({**cfg, "video_removal_mode": "move"})
    assert c.post("/api/bin-check", json={"files": ["p2.jpg"], "action": "convert"}).json()["applies"] is False


def test_run_selected_refused_when_too_big(env):
    root, c, st, started = env
    st["cap"] = 4 * GB                                   # 3 GB > 50% of 4 GB
    r = c.post("/api/run-selected", json={"preview_id": appmod.last_preview["id"], "files": ["p2.jpg"]})
    assert r.status_code == 409 and r.json()["bin_guard"] and vr.BIN_REFUSAL in r.json()["error"]
    assert not started


def test_run_selected_small_ok(env):
    _, c, _, started = env
    r = c.post("/api/run-selected", json={"preview_id": appmod.last_preview["id"], "files": ["p2.jpg"]})
    assert r.status_code == 200 and r.json()["count"] == 1


def test_run_selected_file_count_refusal(env, monkeypatch):
    _, c, _, started = env
    monkeypatch.setenv("L2M_BIN_MAX_FILES", "0")
    r = c.post("/api/run-selected", json={"preview_id": appmod.last_preview["id"], "files": ["p2.jpg"]})
    assert r.status_code == 409 and r.json()["bin_guard"]


def test_cleanup_refused_leaves_movs(env, monkeypatch):
    root, c, st, _ = env
    st["nuke"] = True
    r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["p0.jpg", "p1.jpg"]})
    assert r.status_code == 409 and r.json()["bin_guard"]
    assert (root / "p0.mov").exists() and (root / "p1.mov").exists() and not (root / "_originals").exists()


def test_cleanup_small_goes_to_bin(env, monkeypatch):
    root, c, _, _ = env
    sent = []
    monkeypatch.setattr(vr, "dispose_file", lambda p, mode=None, root=None, folder=None, exts=None: sent.append(Path(p).name) or "trash")
    r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["p0.jpg"]}).json()
    assert r["cleaned"] == 1 and r["details"][0]["action"] == "trash" and sent == ["p0.mov"]
