"""Regression tests for the 2026-11-09 review findings in app.py (cleanup endpoint,
token / Host protection, empty input folder). Temp dirs only."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
from test_bin_guard import env as bin_env, fake_bin  # noqa: E402,F401  (fixtures)
from test_security import env as sec_env  # noqa: E402,F401  (fixture)


@pytest.fixture
def client(sec_env):
    return TestClient(appmod.app, base_url="http://localhost:7000")


# ── 3. DELETE /api/cleanup is gone; cleanup-movs is the only way ──────────────

def test_delete_cleanup_endpoint_removed_and_files_untouched(bin_env):
    root, c, _, _ = bin_env
    r = c.request("DELETE", "/api/cleanup", json={"paths": [str(root / "p0.mov")]})
    assert r.status_code in (404, 405)
    assert (root / "p0.mov").exists()
    for gone in ("/api/cleanup-scan", "/api/cleanup-preview"):
        assert c.get(gone).status_code == 404
    paths = {getattr(route, "path", "") for route in appmod.app.routes}
    assert "/api/cleanup" not in paths and "/api/cleanup-movs" in paths


def test_ui_only_uses_preview_bound_cleanup():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "'/api/cleanup'" not in html and '"/api/cleanup"' not in html
    assert "/api/cleanup-movs" in html
    assert "cleanup-scan" not in html and "cleanup-preview" not in html and "doDeleteSelected" not in html


def test_cleanup_movs_holds_the_run_lock(bin_env, monkeypatch):
    root, c, _, _ = bin_env
    seen = {}

    def fake_cleanup(files, rowmap, cfg, root_):
        seen["running"] = appmod.run_state.running
        seen["second_start"] = appmod.run_state.start("manual")        # a run must be refused meanwhile
        return {"ok": True, "cleaned": 0, "details": [], "refused": [], "errors": [], "skipped": []}
    monkeypatch.setattr(appmod, "cleanup_movs", fake_cleanup)
    monkeypatch.setattr(appmod.run_state, "running", False)
    r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["p0.jpg"]})
    assert r.status_code == 200
    assert seen == {"running": True, "second_start": False}
    assert appmod.run_state.running is False                          # released afterwards


def test_cleanup_movs_refused_while_a_run_is_active(bin_env, monkeypatch):
    root, c, _, _ = bin_env
    monkeypatch.setattr(appmod.run_state, "running", True)
    r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["p0.jpg"]})
    assert r.status_code == 409 and (root / "p0.mov").exists()


def test_cleanup_movs_releases_the_lock_even_if_it_fails(bin_env, monkeypatch):
    root, c, _, _ = bin_env

    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(appmod, "cleanup_movs", boom)
    monkeypatch.setattr(appmod.run_state, "running", False)
    with pytest.raises(RuntimeError):
        c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["p0.jpg"]})
    assert appmod.run_state.running is False


# ── 4. token + Host protection ─────────────────────────────────────────────────

@pytest.fixture
def docker_env(sec_env, monkeypatch):
    monkeypatch.setattr(appmod, "IN_DOCKER", True)
    monkeypatch.delenv("LIVE2MOTION_ALLOWED_HOSTS", raising=False)
    return sec_env


def test_docker_without_token_fails_closed(docker_env):
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    assert c.get("/api/status").status_code == 401


def test_docker_host_header_is_checked_for_dns_rebinding(docker_env, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "tok")
    h = {"X-Live2Motion-Token": "tok"}
    assert TestClient(appmod.app, base_url="http://localhost:7000").get("/api/status", headers=h).status_code == 200
    assert TestClient(appmod.app, base_url="http://192.168.1.20:7000").get("/api/status", headers=h).status_code == 200
    evil = TestClient(appmod.app, base_url="http://rebind.evil.example:7000")
    assert evil.get("/api/status", headers=h).status_code == 403            # even with the right token


def test_allowed_hosts_env_extends_the_allowlist(docker_env, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "tok")
    monkeypatch.setenv("LIVE2MOTION_ALLOWED_HOSTS", "nas.lan, photos.example.org")
    h = {"X-Live2Motion-Token": "tok"}
    for name in ("nas.lan", "photos.example.org"):
        assert TestClient(appmod.app, base_url=f"http://{name}:7000").get("/api/status", headers=h).status_code == 200
    assert TestClient(appmod.app, base_url="http://other.lan:7000").get("/api/status", headers=h).status_code == 403


def test_non_loopback_bind_requires_token_and_checks_host(env_nonloop):
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"X-Live2Motion-Token": "tok"}).status_code == 200
    evil = TestClient(appmod.app, base_url="http://evil.example")
    assert evil.get("/api/status", headers={"X-Live2Motion-Token": "tok"}).status_code == 403


@pytest.fixture
def env_nonloop(sec_env, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_HOST", "0.0.0.0")
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "tok")
    monkeypatch.delenv("LIVE2MOTION_ALLOWED_HOSTS", raising=False)
    return sec_env


def test_loopback_native_still_needs_no_token(client):
    assert client.get("/api/status").status_code == 200


def test_token_auto_generated_saved_private_and_usable(sec_env, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_HOST", "0.0.0.0")
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    token = appmod.ensure_token("0.0.0.0")
    assert token and len(token) >= 32
    cfg_file = sec_env / "config.json"
    assert json.loads(cfg_file.read_text())["access_token"] == token
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(cfg_file).st_mode) == 0o600
    assert appmod.ensure_token("0.0.0.0") is None                    # already configured: nothing regenerated
    assert appmod.get_token() == token
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"X-Live2Motion-Token": token}).status_code == 200
    # saving settings from the UI keeps the generated token
    root = sec_env / "photos"
    root.mkdir()
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(root))
    r = c.post("/api/config", json={**appmod.DEFAULT_CONFIG, "input_directory": str(root)},
               headers={"X-Live2Motion-Token": token})
    assert r.status_code == 200 and json.loads(cfg_file.read_text())["access_token"] == token


def test_no_token_needed_and_none_generated_on_loopback(sec_env):
    assert appmod.ensure_token("127.0.0.1") is None
    assert not (sec_env / "config.json").exists()


def test_refuses_to_start_when_no_token_can_be_saved(sec_env, monkeypatch, capsys):
    monkeypatch.setenv("LIVE2MOTION_HOST", "0.0.0.0")
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)

    def readonly(token):
        raise PermissionError("read-only config")
    monkeypatch.setattr(appmod, "persist_token", readonly)
    with pytest.raises(SystemExit) as e:
        appmod.ensure_token("0.0.0.0")
    assert e.value.code == 1 and "Refusing to start" in capsys.readouterr().err


def test_config_save_is_private(sec_env):
    appmod.save_config({"a": 1})
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(sec_env / "config.json").st_mode) == 0o600


def test_ui_accepts_token_link():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "searchParams.get('token')" in html and "history.replaceState" in html


# ── 7. empty input folder is "not configured" ─────────────────────────────────

def test_empty_input_never_widens_to_home_or_pictures(sec_env, monkeypatch):
    home = sec_env / "home"
    (home / "Pictures").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(appmod, "windows_pictures_dir", lambda: None)
    (sec_env / "config.json").write_text(json.dumps({"input_directory": "   "}))
    assert not appmod.load_config()["input_directory"].strip()
    with pytest.raises(ValueError, match="No input folder"):
        appmod.input_root(appmod.load_config())
    assert appmod.DEFAULT_CONFIG["input_directory"] in ("", "/data")


def test_run_endpoint_refuses_empty_input(client, sec_env, monkeypatch):
    (sec_env / "config.json").write_text(json.dumps({"input_directory": ""}))
    started = []
    monkeypatch.setattr(appmod, "do_run", lambda *a, **k: started.append(a))
    r = client.post("/api/run")
    assert r.status_code == 400 and "No input folder" in r.json()["error"]
    assert not started


def test_do_run_refuses_empty_input_for_schedule_and_watch(monkeypatch):
    monkeypatch.setattr(appmod, "load_config", lambda: {**appmod.DEFAULT_CONFIG, "input_directory": ""})
    st = appmod.RunState()
    monkeypatch.setattr(appmod, "run_state", st)
    spawned = []
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    for trigger in ("schedule", "watch", "manual"):
        appmod.do_run(trigger)
    assert not spawned and not st.running
    assert any("No input folder" in line for line in st.lines)
    assert [h["exit_code"] for h in st.history] == [2, 2, 2]


def test_schedule_and_watch_not_started_without_input(monkeypatch):
    jobs = []
    monkeypatch.setattr(appmod.scheduler, "add_job", lambda *a, **k: jobs.append(a))
    appmod.apply_schedule({"schedule_enabled": True, "schedule_cron": "0 2 * * *", "input_directory": ""})
    assert not jobs
    observed = []
    monkeypatch.setattr(appmod, "Observer", lambda: observed.append(1))
    appmod.start_watcher({"watch_enabled": True, "input_directory": ""})
    assert not observed


def test_saving_schedule_or_watch_needs_an_input_folder(client, sec_env):
    for key in ("schedule_enabled", "watch_enabled"):
        r = client.post("/api/config", json={**appmod.DEFAULT_CONFIG, "input_directory": "", key: True})
        assert r.status_code == 400 and "input folder" in r.json()["error"]


def test_selected_conversion_and_cleanup_refuse_empty_input(sec_env):
    with pytest.raises(ValueError, match="No input folder"):
        appmod.validate_selected("x", ["a.jpg"], {"input_directory": ""}, {"id": "x", "fingerprint": appmod.preview_fingerprint({"input_directory": ""})})


# ── round 2 ────────────────────────────────────────────────────────────────────

def test_unparsable_config_is_never_overwritten(sec_env, monkeypatch):
    cfg = sec_env / "config.json"
    cfg.write_text('{"access_token": "mine", oops')
    before = cfg.read_bytes()
    monkeypatch.setenv("LIVE2MOTION_HOST", "0.0.0.0")
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        appmod.ensure_token("0.0.0.0")                     # refuses to start instead of generating + saving
    with pytest.raises(appmod.ConfigUnreadable):
        appmod.save_config({"a": 1})
    assert appmod.remember_folder("input_directory", str(sec_env)) is False
    assert cfg.read_bytes() == before and not (sec_env / "config.json.tmp").exists()


def test_api_save_refused_for_unparsable_config(client, sec_env, monkeypatch):
    root = sec_env / "photos"
    root.mkdir()
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(root))
    (sec_env / "config.json").write_text("not json")
    r = client.post("/api/config", json={**appmod.DEFAULT_CONFIG, "input_directory": str(root)})
    assert r.status_code == 409 and (sec_env / "config.json").read_text() == "not json"


def test_config_rejects_output_inside_input_and_removal_folder_equal_to_input(sec_env, monkeypatch):
    root = sec_env / "photos"
    (root / "out").mkdir(parents=True)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(sec_env))
    cfg = {"input_directory": str(root), "output_directory": str(root / "out"), "recursive": True}
    with pytest.raises(ValueError, match="inside the input folder"):
        appmod.validate_config(cfg)
    appmod.validate_config({**cfg, "recursive": False})
    for folder in (".", str(root)):
        with pytest.raises(ValueError, match="cannot be the input folder"):
            appmod.check_config_paths({"input_directory": str(root), "video_removal_folder": folder})
    appmod.check_config_paths({"input_directory": str(root), "video_removal_folder": "_originals"})


# ── round 3: suggested input folder (UI pre-fill only) ────────────────────────

def test_suggested_input_is_pictures_folder_or_empty(sec_env, monkeypatch):
    home = sec_env / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(appmod, "windows_pictures_dir", lambda: None)
    assert appmod.suggested_input_dir() == ""                        # no Pictures folder: no fallback to home
    (home / "Pictures").mkdir(parents=True)
    assert appmod.suggested_input_dir() == str(home / "Pictures")
    redirected = sec_env / "OneDrive" / "Pictures"
    redirected.mkdir(parents=True)
    monkeypatch.setattr(appmod, "windows_pictures_dir", lambda: redirected)     # known folder wins
    assert appmod.suggested_input_dir() == str(redirected)
    monkeypatch.setattr(appmod, "windows_pictures_dir", lambda: sec_env / "gone")  # known folder missing
    assert appmod.suggested_input_dir() == ""
    monkeypatch.setattr(appmod, "IN_DOCKER", True)
    assert appmod.suggested_input_dir() == ""


def test_known_folder_failure_gives_none(monkeypatch):
    monkeypatch.setattr(appmod.sys, "platform", "win32")
    import types
    boom = types.SimpleNamespace(windll=types.SimpleNamespace(shell32=types.SimpleNamespace(
        SHGetKnownFolderPath=lambda *a: 1)))
    monkeypatch.setitem(sys.modules, "ctypes", types.SimpleNamespace(
        Structure=object, c_ubyte=int, c_wchar_p=lambda: None, POINTER=lambda x: x, byref=lambda x: x,
        windll=boom.windll))
    assert appmod.windows_pictures_dir() is None


def test_suggestion_is_exposed_but_never_used_by_the_server(client, sec_env, monkeypatch):
    home = sec_env / "home"
    (home / "Pictures").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(appmod, "windows_pictures_dir", lambda: None)
    cfg = client.get("/api/config").json()
    assert cfg["suggested_input_directory"] == str(home / "Pictures") and cfg["input_directory"] == ""
    assert client.post("/api/run").status_code == 400                      # still refuses an empty input
    assert appmod.load_config()["input_directory"] == ""
    jobs = []
    monkeypatch.setattr(appmod.scheduler, "add_job", lambda *a, **k: jobs.append(a))
    appmod.apply_schedule(appmod.load_config() | {"schedule_enabled": True})
    assert not jobs
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "suggested_input_directory" in html


def test_suggestion_only_until_a_config_was_ever_saved(client, sec_env, monkeypatch):
    home = sec_env / "home"
    (home / "Pictures").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(appmod, "windows_pictures_dir", lambda: None)
    assert client.get("/api/config").json()["suggested_input_directory"] == str(home / "Pictures")
    (sec_env / "config.json").write_text(json.dumps({"input_directory": ""}))       # saved once, field left empty
    assert client.get("/api/config").json()["suggested_input_directory"] == ""


def test_cleanup_movs_refused_when_the_preview_has_unreadable_rows(bin_env):
    root, c, _, _ = bin_env
    rows = [{"file": "p0.jpg", "status": "skipped", "kind": "motion_leftover", "video": "p0.mov"},
            {"file": "bad.heic", "status": "skipped", "kind": "skipped", "reason": "unreadable: Error: File is empty"}]
    appmod.set_preview(appmod.preview_fingerprint(appmod.load_config()), rows)
    r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["p0.jpg"]})
    assert r.status_code == 409 and "unreadable" in r.json()["error"] and r.json()["unreadable"] == ["bad.heic"]
    assert (root / "p0.mov").exists() and not (root / "_originals").exists()
    assert appmod.run_state.running is False
