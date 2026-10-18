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
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "IN_DOCKER", False)
    for var in ("LIVE2MOTION_TOKEN", "LIVE2MOTION_HOST"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(root))
    monkeypatch.setattr(appmod, "apply_schedule", lambda cfg: None)
    monkeypatch.setattr(appmod, "start_watcher", lambda cfg: None)
    monkeypatch.setattr(appmod, "stop_watcher", lambda: None)
    calls = []
    monkeypatch.setattr(appmod, "do_run", lambda *a: calls.append(a))
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    c.root, c.calls = root, calls
    return c


def test_build_extract_cmd():
    cmd = appmod.build_extract_cmd({"input_directory": "/in", "output_directory": "/out", "recursive": True,
                                    "extract_mov": True, "extract_still": True, "dry_run": True})
    assert cmd[-9:] == ["--extract", "--input-directory", "/in", "--output-directory", "/out",
                        "--recursive", "--extract-mov", "--extract-still", "--dry-run"]
    assert "--input-image" in appmod.build_extract_cmd({"input_file": "/a.jpg", "input_directory": "/in"})


def test_extract_endpoint_starts_run_with_dry_run(client):
    r = client.post("/api/extract?dry_run=true", json={"input_directory": str(client.root), "extract_mov": True})
    assert r.status_code == 200
    import time
    for _ in range(50):
        if client.calls:
            break
        time.sleep(0.02)
    trigger, dry, overrides = client.calls[0]
    assert trigger == "extract" and dry is True
    assert overrides == {"input_directory": str(client.root), "extract_mov": True}


def test_extract_endpoint_rejects_paths_outside_browse_roots(client, tmp_path):
    assert client.post("/api/extract", json={"output_directory": "/etc"}).status_code == 400
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"x")
    assert client.post("/api/extract", json={"input_file": str(outside)}).status_code == 400
    assert not client.calls


def test_extract_endpoint_rejects_bad_body(client):
    assert client.post("/api/extract", json={"bogus": 1}).status_code == 400
    assert client.post("/api/extract", json={"recursive": "yes"}).status_code == 400


def test_extract_endpoint_needs_token_and_same_origin(client, monkeypatch):
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "s3cret")
    assert client.post("/api/extract").status_code == 401
    assert client.post("/api/extract", headers={"X-Live2Motion-Token": "s3cret", "Origin": "http://evil.example"}).status_code == 403


def test_extract_endpoint_conflicts_while_running(client, monkeypatch):
    monkeypatch.setattr(appmod.run_state, "running", True)
    assert client.post("/api/extract").status_code == 409
