import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated config file + loopback app with no token."""
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "IN_DOCKER", False)
    for var in ("LIVE2MOTION_TOKEN", "LIVE2MOTION_HOST", "LIVE2MOTION_BROWSE_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(appmod, "apply_schedule", lambda cfg: None)
    monkeypatch.setattr(appmod, "start_watcher", lambda cfg: None)
    monkeypatch.setattr(appmod, "stop_watcher", lambda: None)
    return tmp_path


@pytest.fixture
def client(env):
    return TestClient(appmod.app, base_url="http://localhost:7000")


# ── token ──────────────────────────────────────────────────────────────────────

def test_no_token_means_no_auth(client):
    assert client.get("/api/status").status_code == 200


def test_token_required_on_api_but_not_page(client, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "s3cret")
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Live2Motion-Token": "nope"}).status_code == 401
    assert client.get("/api/status", headers={"X-Live2Motion-Token": "s3cret"}).status_code == 200


def test_token_from_config_file(client, env):
    (env / "config.json").write_text('{"access_token": "abc"}')
    assert client.get("/api/config").status_code == 401
    r = client.get("/api/config", headers={"X-Live2Motion-Token": "abc"})
    assert r.status_code == 200 and "access_token" not in r.json()


def test_query_token_only_for_progress_stream(client, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "s3cret")
    assert client.get("/api/status?token=s3cret").status_code == 401


# ── origin / CSRF ──────────────────────────────────────────────────────────────

def test_cross_origin_post_rejected(client):
    r = client.post("/api/cancel", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_same_origin_and_no_origin_post_allowed(client):
    assert client.post("/api/cancel", headers={"Origin": "http://localhost:7000"}).status_code == 400  # "Not running"
    assert client.post("/api/cancel").status_code == 400


def test_cross_origin_get_still_allowed_by_origin_check(client):
    assert client.get("/api/status", headers={"Origin": "http://evil.example"}).status_code == 200


def test_foreign_host_header_rejected_when_loopback_only(env):
    c = TestClient(appmod.app, base_url="http://evil.example")
    assert c.get("/api/status").status_code == 403


# ── browse ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def tree(env, monkeypatch):
    allowed = env / "photos"
    (allowed / "a").mkdir(parents=True)
    outside = env / "outside"
    (outside / "secret").mkdir(parents=True)
    (allowed / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(allowed))
    return allowed, outside


def test_browse_inside_root(client, tree):
    allowed, _ = tree
    r = client.get("/api/browse", params={"path": str(allowed)})
    assert r.status_code == 200
    assert r.json()["dirs"] == ["a"]          # escaping symlink hidden
    assert r.json()["parent"] is None          # can't walk above the root


def test_browse_outside_root_rejected(client, tree):
    _, outside = tree
    assert client.get("/api/browse", params={"path": str(outside)}).status_code == 403
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 403


def test_browse_dotdot_and_symlink_escape_rejected(client, tree):
    allowed, outside = tree
    assert client.get("/api/browse", params={"path": f"{allowed}/a/../../outside"}).status_code == 403
    assert client.get("/api/browse", params={"path": f"{allowed}/link/secret"}).status_code == 403


def test_default_roots_follow_config(env):
    inp = env / "home" / "me" / "photos" / "2024"
    inp.mkdir(parents=True)
    (env / "config.json").write_text(f'{{"input_directory": "{inp}"}}')
    roots = appmod.browse_roots()
    assert inp.resolve() in roots and inp.parent.resolve() in roots
    assert Path("/") not in roots


# ── config validation ──────────────────────────────────────────────────────────

def good(**kw):
    return {**appmod.DEFAULT_CONFIG, **kw}


def test_valid_config_saved_and_server_keys_preserved(client, env):
    (env / "config.json").write_text('{"access_token": "abc", "host": "127.0.0.1"}')
    h = {"X-Live2Motion-Token": "abc"}
    r = client.post("/api/config", json=good(video_removal_mode="move"), headers=h)
    assert r.status_code == 200
    saved = appmod.json.loads((env / "config.json").read_text())
    assert saved["video_removal_mode"] == "move" and saved["access_token"] == "abc"


@pytest.mark.parametrize("bad", [
    {"bogus": 1},
    {"recursive": "yes"},
    {"watch_debounce": "5"},
    {"watch_debounce": True},
    {"watch_debounce": -1},
    {"input_directory": 5},
    {"input_directory": ["/x"]},
    {"video_removal_mode": "shred"},
    {"schedule_cron": "not a cron"},
    {"host": "0.0.0.0"},
    {"access_token": "x"},
])
def test_bad_config_rejected(client, env, bad):
    r = client.post("/api/config", json=bad)
    assert r.status_code == 400 and r.json()["error"]
    assert not (env / "config.json").exists()


def test_non_object_config_rejected(client):
    assert client.post("/api/config", json=[1, 2]).status_code == 400
    assert client.post("/api/config", content=b"{nope", headers={"Content-Type": "application/json"}).status_code == 400


# ── config paths limited to browse roots ───────────────────────────────────────

@pytest.mark.parametrize("key", ["input_directory", "output_directory", "video_removal_folder"])
def test_config_paths_outside_roots_rejected(client, env, monkeypatch, key):
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(env / "ok"))
    (env / "ok").mkdir()
    r = client.post("/api/config", json=good(**{key: "/etc"}))
    assert r.status_code == 400 and "outside" in r.json()["error"]
    assert not (env / "config.json").exists()


def test_config_paths_inside_roots_accepted(client, env, monkeypatch):
    (env / "ok" / "in").mkdir(parents=True)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(env / "ok"))
    cfg = good(input_directory=str(env / "ok" / "in"), output_directory="", video_removal_folder="_moved")
    assert client.post("/api/config", json=cfg).status_code == 200
    cfg["video_removal_folder"] = "../../elsewhere"
    assert client.post("/api/config", json=cfg).status_code == 400


# ── access log redaction ───────────────────────────────────────────────────────

def test_access_log_redacts_token():
    import logging
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/progress?from_line=0&token=s3cret&x=1", "1.1", 200), None)
    assert appmod.RedactTokenFilter().filter(rec)
    out = rec.getMessage()
    assert "s3cret" not in out and "token=***&x=1" in out
