"""Desktop / Microsoft Store shell (desktop/live2motion_desktop.py) and the small app.py
hooks it relies on (LIVE2MOTION_DATA_DIR, frozen converter command, excluded dirs)."""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))

pytest.importorskip("httpx")

import app as appmod  # noqa: E402
import live2motion_desktop as desk  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_run_state(monkeypatch):
    """stop_server / begin_shutdown mark the shared run_state as closing: undo it after each test."""
    for attr in ("closing", "running", "process", "cancel_requested", "cancel_file"):
        monkeypatch.setattr(appmod.run_state, attr, getattr(appmod.run_state, attr))


# ─── app.py hooks ───────────────────────────────────────────────────────────────

def test_data_dir_env_moves_config_preview_and_port(tmp_path):
    env = {**os.environ, "LIVE2MOTION_DATA_DIR": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c",
         "import app; print(app.CONFIG_FILE); print(app.preview_file()); print(app.PORT_FILE)"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == [str(tmp_path / "config.json"), str(tmp_path / "preview.json"), str(tmp_path / ".port")]


def test_default_data_dir_is_app_folder():
    if os.environ.get("LIVE2MOTION_DATA_DIR"):
        pytest.skip("LIVE2MOTION_DATA_DIR set in the environment")
    assert appmod.CONFIG_FILE == appmod.BASE_DIR / "config.json"


def test_motionphoto2_cmd_source_frozen_and_override(monkeypatch):
    monkeypatch.delenv("MOTIONPHOTO2_BIN", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert appmod.motionphoto2_cmd() == [sys.executable, str(appmod.BASE_DIR / "MotionPhoto2" / "motionphoto2.py")]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert appmod.motionphoto2_cmd() == [sys.executable, "--motionphoto2"]
    monkeypatch.setenv("MOTIONPHOTO2_BIN", "/opt/mp2")
    assert appmod.motionphoto2_cmd() == ["/opt/mp2"]


def test_bin_entry_point_skips_flags(monkeypatch):
    monkeypatch.setattr(appmod, "BIN", ["/x/Live2Motion.exe", "--motionphoto2"])
    assert appmod.bin_entry_point() == "/x/Live2Motion.exe"
    monkeypatch.setattr(appmod, "BIN", ["/usr/bin/python3", "/x/motionphoto2.py"])
    assert appmod.bin_entry_point() == "/x/motionphoto2.py"
    assert appmod.build_cmd({"input_directory": "/in"})[:2] == ["/usr/bin/python3", "/x/motionphoto2.py"]


def test_excluded_dirs_are_never_browsable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    appdata = home / "AppData"
    (appdata / "Local" / "x").mkdir(parents=True)
    (home / "Pictures").mkdir()
    roots = [home.resolve()]
    assert appmod.is_browsable((appdata / "Local" / "x").resolve(), roots)
    monkeypatch.setenv("LIVE2MOTION_EXCLUDED_DIRS", str(appdata))
    assert not appmod.is_browsable(appdata.resolve(), roots)
    assert not appmod.is_browsable((appdata / "Local" / "x").resolve(), roots)
    assert appmod.is_browsable((home / "Pictures").resolve(), roots)
    assert appmod.is_browsable(home.resolve(), roots)


def test_excluded_dirs_refuse_config_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    bad = home / "AppData" / "Local" / "photos"
    bad.mkdir(parents=True)
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "browse_roots", lambda: [home.resolve()])
    appmod.check_config_paths({"output_directory": str(bad)})          # allowed without exclusion
    monkeypatch.setenv("LIVE2MOTION_EXCLUDED_DIRS", str(home / "AppData"))
    with pytest.raises(ValueError):
        appmod.check_config_paths({"output_directory": str(bad)})
    with pytest.raises(ValueError):
        appmod.check_config_paths({"input_directory": str(bad)})


# ─── desktop shell ──────────────────────────────────────────────────────────────

def test_data_dir_uses_localappdata(tmp_path):
    assert desk.data_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "Live2Motion"
    assert desk.data_dir({"LIVE2MOTION_DATA_DIR": str(tmp_path / "d"), "LOCALAPPDATA": "x"}) == tmp_path / "d"


def test_configure_environment_loopback_token_and_logs(tmp_path):
    env = {"PATH": "/usr/bin", "LIVE2MOTION_HOST": "0.0.0.0", "LOCALAPPDATA": str(tmp_path / "L"),
           "APPDATA": str(tmp_path / "R")}
    desk.configure_environment(tmp_path, "tok", packaged=False, env=env)
    assert env["LIVE2MOTION_DATA_DIR"] == str(tmp_path)
    assert env["LIVE2MOTION_HOST"] == "127.0.0.1"
    assert env["LIVE2MOTION_TOKEN"] == "tok"
    assert env["LIVE2MOTION_LOG_FILE"] == str(tmp_path / "logs" / "live2motion.log")
    assert "LIVE2MOTION_EXCLUDED_DIRS" not in env
    desk.configure_environment(tmp_path, "tok", packaged=True, env=env)
    excluded = env["LIVE2MOTION_EXCLUDED_DIRS"].split(os.pathsep)
    assert str(tmp_path / "L") in excluded and str(tmp_path / "R") in excluded


def test_configure_environment_keeps_user_log_file(tmp_path):
    env = {"LIVE2MOTION_LOG_FILE": "/elsewhere.log"}
    desk.configure_environment(tmp_path, "t", packaged=False, env=env)
    assert env["LIVE2MOTION_LOG_FILE"] == "/elsewhere.log"


def test_bind_loopback_socket_prefers_port_then_falls_back():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        s = desk.bind_loopback_socket(port)
        try:
            host, got = s.getsockname()
            assert host == "127.0.0.1" and got != port and got > 0
        finally:
            s.close()
    finally:
        busy.close()


@pytest.mark.skipif(sys.platform == "win32", reason="lock-file variant")
def test_single_instance(tmp_path):
    a, b = desk.SingleInstance(tmp_path), desk.SingleInstance(tmp_path)
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_run_motionphoto2_dispatch(tmp_path, monkeypatch, capsys):
    mp = tmp_path / "MotionPhoto2"
    mp.mkdir()
    (mp / "motionphoto2.py").write_text("import sys\nprint('ARGS', sys.argv[1:])\nsys.exit(3)\n")
    monkeypatch.setattr(desk, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    assert desk.main(["--motionphoto2", "--input-directory", "x"]) == 3
    assert "ARGS ['--input-directory', 'x']" in capsys.readouterr().out


def test_server_serves_ui_requires_token_and_stops(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setenv("LIVE2MOTION_HOST", "127.0.0.1")
    monkeypatch.setenv("LIVE2MOTION_TOKEN", "sessiontoken")
    sock = desk.bind_loopback_socket(0)
    port = sock.getsockname()[1]
    server, thread = desk.start_server(appmod.app, sock)
    try:
        assert desk.wait_started(server, thread, 20)
        base = f"http://127.0.0.1:{port}"
        r = httpx.get(base + "/")
        assert r.status_code == 200 and "Live2Motion" in r.text
        assert httpx.get(base + "/api/status").status_code == 401
        assert httpx.get(base + "/api/status", headers={"X-Live2Motion-Token": "sessiontoken"}).status_code == 200
        assert httpx.get(base + "/api/status", headers={"Host": "evil.example",
                                                        "X-Live2Motion-Token": "sessiontoken"}).status_code == 403
    finally:
        assert desk.stop_server(server, thread, appmod)
    assert not thread.is_alive()


