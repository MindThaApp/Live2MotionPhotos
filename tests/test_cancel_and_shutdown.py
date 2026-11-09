"""Cooperative cancel (F1), shutdown without unattended force-kills (F2), one path check
for every run (F3), wider exclusions and walks that skip them (F4), and the small
desktop fixes (F5-F7). Only generated temp files are used."""
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))
sys.path.insert(0, str(ROOT / "desktop"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")
pytest.importorskip("httpx")

import extract  # noqa: E402
import video_removal  # noqa: E402
import Muxer as muxer_mod  # noqa: E402
import motionphoto2 as mp  # noqa: E402
import run_cancel  # noqa: E402
from test_preview_filters import JPEG, VIDEO, FakeET, motion_photo_bytes  # noqa: E402

import app as appmod  # noqa: E402
import live2motion_desktop as desk  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    run_cancel.reset()
    monkeypatch.delenv(run_cancel.CANCEL_FILE_ENV, raising=False)
    monkeypatch.delenv(run_cancel.STATUS_FILE_ENV, raising=False)
    monkeypatch.delenv("LIVE2MOTION_EXCLUDED_DIRS", raising=False)
    for attr in ("closing", "running", "process", "cancel_requested", "cancel_file", "status_file"):
        monkeypatch.setattr(appmod.run_state, attr, getattr(appmod.run_state, attr))
    monkeypatch.setattr(appmod.run_state, "closing", False)
    yield
    run_cancel.reset()


def make_pairs(root, n=3):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / f"p{i}.jpg").write_bytes(JPEG)
        (root / f"p{i}.mov").write_bytes(VIDEO)


def run_engine(root, monkeypatch, capsys, *extra):
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), *extra])
    monkeypatch.setenv("HOME", str(root.parent))
    code = 0
    try:
        mp.main()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    rows = [json.loads(line[9:]) for line in out.splitlines() if line.startswith("@@RESULT ")]
    return code, out, rows


def no_temp_left(root):
    return not [p for p in Path(root).rglob("*") if p.name.startswith(".mp2")]


# ─── engine: run_cancel unit ────────────────────────────────────────────────────

def test_checkpoint_only_stops_when_asked_and_never_inside_a_file(tmp_path, monkeypatch, capsys):
    flag = tmp_path / "stop.flag"
    run_cancel.checkpoint()                               # no variable: nothing happens
    monkeypatch.setenv(run_cancel.CANCEL_FILE_ENV, str(flag))
    run_cancel.checkpoint()                               # flag not there yet
    flag.write_text("")
    with run_cancel.critical():
        run_cancel.checkpoint()                           # inside one file's work: never stops
    with pytest.raises(SystemExit) as e:
        run_cancel.checkpoint()
    assert e.value.code == run_cancel.CANCELLED_EXIT
    assert "[CANCELLED]" in capsys.readouterr().out


def test_sigterm_outside_a_file_exits_at_once_inside_waits():
    with pytest.raises(SystemExit) as e:
        run_cancel.on_sigterm()
    assert e.value.code == 1
    run_cancel.reset()
    with run_cancel.critical():
        run_cancel.on_sigterm()                           # deferred: no exception here
        assert run_cancel.requested()
    with pytest.raises(SystemExit) as e:
        run_cancel.checkpoint()
    assert e.value.code == run_cancel.CANCELLED_EXIT


# ─── engine: every safe point ───────────────────────────────────────────────────

def test_cancel_before_the_first_file_touches_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    make_pairs(root)
    flag = tmp_path / "stop.flag"
    flag.write_text("")
    monkeypatch.setenv(run_cancel.CANCEL_FILE_ENV, str(flag))
    code, out, rows = run_engine(root, monkeypatch, capsys, "--overwrite", "--delete-video",
                                 "--video-removal-mode", "move")
    assert code == run_cancel.CANCELLED_EXIT and "[CANCELLED]" in out
    assert not [r for r in rows if r["status"] == "converted"]
    for i in range(3):
        assert (root / f"p{i}.jpg").read_bytes() == JPEG and (root / f"p{i}.mov").read_bytes() == VIDEO
    assert not (root / "_originals").exists() and no_temp_left(root)


def _cancel_during_set_aside(monkeypatch, trigger):
    """Raise the stop request while the original photo is being set aside: the worst moment."""
    real = muxer_mod.dispose_file
    calls = []

    def dispose(path, *a, **k):
        calls.append(Path(path).name)
        if len(calls) == 1:
            trigger()
        return real(path, *a, **k)
    monkeypatch.setattr(muxer_mod, "dispose_file", dispose)
    return calls


@pytest.mark.parametrize("match", [[], ["--exif-match"]], ids=["by-name", "by-exif"])
def test_cancel_during_overwrite_finishes_that_photo_then_stops(tmp_path, monkeypatch, capsys, match):
    root = tmp_path / "in"
    make_pairs(root)
    flag = tmp_path / "stop.flag"
    monkeypatch.setenv(run_cancel.CANCEL_FILE_ENV, str(flag))
    status = tmp_path / "l2m_status_test.txt"
    monkeypatch.setenv(run_cancel.STATUS_FILE_ENV, str(status))
    seen_status = []

    def trigger():
        flag.write_text("")
        seen_status.append(status.read_text().split()[0])
        # a safe point reached while this photo is being set aside (as if a nested helper
        # called it): it must not stop the run here. Without critical() around the photo's
        # work this exits mid-file and the asserts below fail.
        run_cancel.checkpoint()
    calls = _cancel_during_set_aside(monkeypatch, trigger)
    FakeET.ids = {f"p{i}.{e}": f"ID{i}" for i in range(3) for e in ("jpg", "mov")} if match else {}
    try:
        code, out, rows = run_engine(root, monkeypatch, capsys, "--overwrite", "--delete-video",
                                     "--video-removal-mode", "move", *match)
    finally:
        FakeET.ids = {}
    assert code == run_cancel.CANCELLED_EXIT, out
    assert len(calls) == 1                                # only one photo was started
    first = Path(calls[0]).stem
    # the started photo went all the way: merged photo in place, original + video set aside
    assert (root / f"{first}.jpg").read_bytes().startswith(JPEG)
    assert len((root / f"{first}.jpg").read_bytes()) > len(JPEG)
    assert (root / "_originals" / f"{first}.jpg").read_bytes() == JPEG
    assert (root / "_originals" / f"{first}.mov").read_bytes() == VIDEO
    assert [r["file"] for r in rows if r["status"] == "converted"] == [f"{first}.jpg"]
    for i in range(3):                                    # the others were never touched
        if f"p{i}" != first:
            assert (root / f"p{i}.jpg").read_bytes() == JPEG and (root / f"p{i}.mov").read_bytes() == VIDEO
    assert no_temp_left(root)
    assert seen_status == [run_cancel.BUSY]                       # the app was told: inside a file
    assert status.read_text().startswith(run_cancel.IDLE)         # and told again once it was done


def test_status_file_says_busy_only_inside_one_file(tmp_path, monkeypatch):
    status = tmp_path / "l2m_status_test.txt"
    monkeypatch.setenv(run_cancel.STATUS_FILE_ENV, str(status))
    run_cancel.checkpoint()
    assert status.read_text().startswith(run_cancel.IDLE)
    with run_cancel.critical():
        assert status.read_text().startswith(run_cancel.BUSY)
        with run_cancel.critical():                               # nested: still busy
            pass
        assert status.read_text().startswith(run_cancel.BUSY)
    assert status.read_text().startswith(run_cancel.IDLE)
    assert appmod.converter_status(str(status))[0] == run_cancel.IDLE
    status.write_text("")                                         # caught mid-rewrite: assume busy
    assert appmod.converter_busy(str(status))
    assert appmod.converter_status(str(tmp_path / "none.txt")) == (None, None)
    other = tmp_path / "photo.jpg"                                # never rewrites any other file
    other.write_bytes(JPEG)
    monkeypatch.setenv(run_cancel.STATUS_FILE_ENV, str(other))
    run_cancel.reset()
    with run_cancel.critical():
        pass
    assert other.read_bytes() == JPEG


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal")
def test_sigterm_during_overwrite_is_deferred_until_the_photo_is_in_place(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    make_pairs(root)
    old = signal.getsignal(signal.SIGTERM)
    run_cancel.install_sigterm_handler()
    try:
        calls = _cancel_during_set_aside(monkeypatch, lambda: os.kill(os.getpid(), signal.SIGTERM))
        code, out, rows = run_engine(root, monkeypatch, capsys, "--overwrite", "--video-removal-mode", "move")
    finally:
        signal.signal(signal.SIGTERM, old)
    assert code == run_cancel.CANCELLED_EXIT, out
    assert len(calls) == 1
    first = Path(calls[0]).stem
    assert len((root / f"{first}.jpg").read_bytes()) > len(JPEG)    # published, not left as .part
    assert (root / "_originals" / f"{first}.jpg").read_bytes() == JPEG
    others = [f"p{i}.jpg" for i in range(3) if f"p{i}" != first]
    assert all((root / o).read_bytes() == JPEG for o in others) and no_temp_left(root)


def test_cancel_between_copied_unmuxed_files(tmp_path, monkeypatch, capsys):
    root, out_dir = tmp_path / "in", tmp_path / "out"
    root.mkdir()
    out_dir.mkdir()
    for n in ("a.txt", "b.txt", "c.txt"):
        (root / n).write_text(n)
    flag = tmp_path / "stop.flag"
    monkeypatch.setenv(run_cancel.CANCEL_FILE_ENV, str(flag))
    real = mp.copy_unmuxed_file
    copied = []

    def copy(src, dest, verbose=False):
        copied.append(Path(src).name)
        flag.write_text("")                               # ask to stop while copying the first file
        return real(src, dest, verbose)
    monkeypatch.setattr(mp, "copy_unmuxed_file", copy)
    code, out, _ = run_engine(root, monkeypatch, capsys, "--output-directory", str(out_dir), "--copy-unmuxed")
    assert code == run_cancel.CANCELLED_EXIT, out
    assert len(copied) == 1 and (out_dir / copied[0]).read_text() == copied[0]   # that copy was finished
    assert len(list(out_dir.iterdir())) == 1 and no_temp_left(out_dir)
    assert sorted(p.name for p in root.iterdir()) == ["a.txt", "b.txt", "c.txt"]


def test_cancel_between_extracted_files(tmp_path, monkeypatch):
    for n in ("a.jpg", "b.jpg"):
        (tmp_path / n).write_bytes(JPEG)
    flag = tmp_path / "stop.flag"
    monkeypatch.setenv(run_cancel.CANCEL_FILE_ENV, str(flag))
    seen = []
    monkeypatch.setattr(extract, "plan_extract", lambda photo, et=None: (seen.append(photo), b"", None)[1:])

    def one(photo, *a, **k):
        flag.write_text("")
        return {"outputs": ["x"], "notes": ["n"]}
    monkeypatch.setattr(extract, "extract_one", one)
    with pytest.raises(SystemExit) as e:
        extract.run_extract(str(tmp_path), None, None, True)
    assert e.value.code == run_cancel.CANCELLED_EXIT and len(seen) == 1


def test_converter_subprocess_honours_the_cancel_file(tmp_path):
    root = tmp_path / "in"
    make_pairs(root, 1)
    flag = tmp_path / "stop.flag"
    flag.write_text("")
    env = {**os.environ, run_cancel.CANCEL_FILE_ENV: str(flag)}     # dry run: writes nothing anywhere
    proc = subprocess.run([sys.executable, str(ROOT / "MotionPhoto2" / "motionphoto2.py"),
                           "--input-directory", str(root), "--dry-run"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == run_cancel.CANCELLED_EXIT, proc.stdout + proc.stderr
    assert "[CANCELLED]" in proc.stdout


# ─── engine: excluded folders are never scanned ─────────────────────────────────

def test_recursive_walk_skips_excluded_dirs(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    make_pairs(home / "AppData" / "Local" / "x", 1)
    make_pairs(home / "Pictures", 1)
    monkeypatch.setenv("LIVE2MOTION_EXCLUDED_DIRS", str(home / "AppData"))
    code, out, rows = run_engine(home, monkeypatch, capsys, "--recursive", "--dry-run")
    files = {r["file"].replace("\\", "/") for r in rows}
    assert "Pictures/p0.jpg" in files and not [f for f in files if f.startswith("AppData")]
    (home / "Pictures" / "m.jpg").write_bytes(JPEG)
    (home / "AppData" / "Local" / "x" / "m.jpg").write_bytes(JPEG)
    found = extract._collect_images(home.resolve(), True)
    assert not [f for f in found if f.startswith("AppData")] and any("Pictures" in f for f in found)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink needs privileges on Windows")
def test_recursive_walk_skips_a_link_into_an_excluded_dir(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    make_pairs(tmp_path / "AppData" / "x", 1)
    make_pairs(home / "Pictures", 1)
    (home / "link").symlink_to(tmp_path / "AppData", target_is_directory=True)
    monkeypatch.setenv("LIVE2MOTION_EXCLUDED_DIRS", str(tmp_path / "AppData"))
    _, _, rows = run_engine(home, monkeypatch, capsys, "--recursive", "--dry-run")
    assert not [r for r in rows if r["file"].replace("\\", "/").startswith("link")]


# ─── app: cooperative cancel and the last-resort watchdog ───────────────────────

FAKE_CONVERTER = textwrap.dedent("""
    import os, sys, time
    flag = os.environ["LIVE2MOTION_CANCEL_FILE"]
    status = os.environ.get("LIVE2MOTION_STATUS_FILE")
    mode = os.environ.get("FAKE_MODE", "cooperative")
    for i in range(400):
        if os.path.exists(flag):
            if mode == "hang":               # stuck between files: silent, never ends
                while True:
                    time.sleep(1)
            if mode == "busy":               # one very slow file (multi-GB move): silent but busy
                with open(status, "w") as fh:
                    fh.write("busy")
                time.sleep(float(os.environ.get("FAKE_BUSY_SECONDS", "3")))
                with open(status, "w") as fh:
                    fh.write("idle")
            print("[CANCELLED] stopped", flush=True)
            sys.exit(4)
        print(f"file {i}", flush=True)
        time.sleep(0.05)
""")


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    script = tmp_path / "fake_mp2.py"
    script.write_text(FAKE_CONVERTER)
    photos = tmp_path / "photos"
    photos.mkdir()
    monkeypatch.setattr(appmod, "BIN", [sys.executable, str(script)])
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "IN_DOCKER", False)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(tmp_path))
    monkeypatch.delenv("LIVE2MOTION_LOG_FILE", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({**appmod.DEFAULT_CONFIG, "input_directory": str(photos)}))
    return tmp_path


def start_run(trigger="manual"):
    t = threading.Thread(target=appmod.do_run, args=(trigger,), daemon=True)
    t.start()
    for _ in range(200):
        if appmod.run_state.process is not None and any("file " in line for line in appmod.run_state.lines):
            return t
        time.sleep(0.05)
    raise AssertionError("fake converter did not start")


def test_cancel_is_cooperative_and_never_force_kills(fake_app, monkeypatch):
    killed = []
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: killed.append(p))
    t = start_run()
    flag = appmod.run_state.cancel_file
    assert flag and not os.path.exists(flag)
    assert appmod.request_cancel()
    assert os.path.exists(flag)
    t.join(20)
    assert not t.is_alive() and not killed
    log = "\n".join(appmod.run_state.lines)
    assert "Stopping after the current file" in log and "Cancelled: stopped after the current file" in log
    assert appmod.run_state.history[0]["exit_code"] == run_cancel.CANCELLED_EXIT
    assert not os.path.exists(flag)                       # the run's own flag file is removed


def test_watchdog_force_stops_only_a_hung_converter(fake_app, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(appmod, "COOPERATIVE_CANCEL_TIMEOUT", 0.5)
    killed = []
    real = appmod.kill_process_tree
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: (killed.append(p), real(p, grace=2)))
    t = start_run()
    appmod.request_cancel()
    t.join(20)
    assert not t.is_alive() and len(killed) == 1
    assert "force-stopping it" in "\n".join(appmod.run_state.lines)


def test_watchdog_never_kills_during_a_long_file(fake_app, monkeypatch):
    """No output for 3 s (6x the timeout) while the converter says it is inside one file:
    never force-stopped; it ends by itself after that file."""
    monkeypatch.setenv("FAKE_MODE", "busy")
    monkeypatch.setattr(appmod, "COOPERATIVE_CANCEL_TIMEOUT", 0.5)
    killed = []
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: killed.append(p))
    t = start_run()
    appmod.request_cancel()
    t.join(30)
    assert not t.is_alive() and not killed
    log = "\n".join(appmod.run_state.lines)
    assert "never force-stopped in the middle of a file" in log and "force-stopping it" not in log
    assert appmod.run_state.history[0]["exit_code"] == run_cancel.CANCELLED_EXIT


def test_shutdown_timeout_never_kills_during_a_long_file(fake_app, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "busy")
    monkeypatch.setattr(appmod, "COOPERATIVE_CANCEL_TIMEOUT", 60)
    killed = []
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: killed.append(p))
    t = start_run()
    proc = appmod.run_state.process
    assert appmod.begin_shutdown(timeout=0.5)               # timeout passes while busy: keeps waiting
    assert proc.returncode == run_cancel.CANCELLED_EXIT and not killed
    t.join(5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_kill_process_tree_waits_for_a_busy_converter_after_sigterm(tmp_path):
    """POSIX: the converter defers SIGTERM inside one file; no SIGKILL while it is busy."""
    status = tmp_path / "l2m_status_test.txt"
    status.write_text("busy")
    script = tmp_path / "busy.py"
    script.write_text(textwrap.dedent(f"""
        import signal, sys, time
        signal.signal(signal.SIGTERM, lambda *a: None)      # deferred, like run_cancel inside critical()
        print("ready", flush=True)
        time.sleep(2)
        open({str(status)!r}, "w").write("idle")
        sys.exit(4)
    """))
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stdout.readline().strip() == "ready"
    appmod.kill_process_tree(proc, grace=0.2, still_busy=lambda: appmod.converter_busy(str(status)))
    assert proc.wait(5) == 4                                 # finished its file, was not SIGKILLed


def test_finished_run_never_clears_the_next_runs_cancel_file(fake_app, monkeypatch):
    """Finding 2: a run that starts right after finish() keeps its own cancel file."""
    real_finish = appmod.run_state.finish

    def finish(code):
        real_finish(code)
        appmod.run_state.cancel_file = "next-run.flag"      # the next run took the lock and set its flag
        appmod.run_state.status_file = "next-run.status"
    monkeypatch.setattr(appmod.run_state, "finish", finish)
    t = start_run()
    appmod.request_cancel()
    t.join(20)
    assert not t.is_alive()
    assert appmod.run_state.cancel_file == "next-run.flag"
    assert appmod.run_state.status_file == "next-run.status"


def test_finished_run_clears_its_own_cancel_file_before_releasing(fake_app, monkeypatch):
    seen = []
    real_finish = appmod.run_state.finish
    monkeypatch.setattr(appmod.run_state, "finish",
                        lambda code: (seen.append(appmod.run_state.cancel_file), real_finish(code)))
    t = start_run()
    appmod.request_cancel()
    t.join(20)
    assert seen == [None] and appmod.run_state.cancel_file is None


def test_api_cancel_does_not_kill(fake_app, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    killed = []
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: killed.append(p))
    t = start_run()
    with TestClient(appmod.app, base_url="http://localhost:7000") as c:
        assert c.post("/api/cancel").json() == {"ok": True}
    t.join(20)
    assert not t.is_alive() and not killed


# ─── app: shutdown ──────────────────────────────────────────────────────────────

def test_shutdown_refuses_new_runs_and_waits_for_the_active_one(fake_app, monkeypatch):
    killed = []
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: killed.append(p))
    t = start_run()
    proc = appmod.run_state.process
    assert appmod.begin_shutdown(timeout=20)
    assert proc.poll() is not None and not killed          # ended by itself, nothing orphaned
    t.join(5)
    n = len(appmod.run_state.history)
    for trigger in ("manual", "schedule", "watch"):
        appmod.do_run(trigger)                             # refused: never starts
    assert len(appmod.run_state.history) == n and not appmod.run_state.running
    assert not appmod.run_state.start("cleanup", reset=False)
    from fastapi.testclient import TestClient
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    with TestClient(appmod.app, base_url="http://localhost:7000") as c:
        r = c.post("/api/run")
        assert r.status_code == 409 and "closing" in r.json()["error"]


def test_closing_while_a_schedule_run_is_starting_runs_nothing(fake_app, monkeypatch):
    """Closing lands after the schedule run took the lock but before the converter started."""
    spawned = []
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    real_check = appmod.assert_run_paths

    def check(cfg):
        real_check(cfg)
        assert appmod.start_closing()                        # the window was closed right now
    monkeypatch.setattr(appmod, "assert_run_paths", check)
    appmod.do_run("schedule")
    assert not spawned and appmod.run_state.history[0]["exit_code"] == run_cancel.CANCELLED_EXIT
    assert appmod.begin_shutdown(timeout=5)
    appmod.do_run("watch")                                   # and nothing starts afterwards
    assert not spawned and not appmod.run_state.running


def test_closing_while_the_converter_is_being_started(fake_app, monkeypatch):
    """Closing lands while Popen runs: the converter is asked to stop at once and does."""
    killed = []
    monkeypatch.setattr(appmod, "kill_process_tree", lambda p, *a, **k: killed.append(p))
    real_popen = appmod.subprocess.Popen

    in_popen = threading.Event()

    def popen(*a, **k):
        appmod.start_closing()                               # process not registered yet
        in_popen.set()
        return real_popen(*a, **k)
    monkeypatch.setattr(appmod.subprocess, "Popen", popen)
    t = threading.Thread(target=appmod.do_run, args=("schedule",), daemon=True)
    t.start()
    assert in_popen.wait(20)
    assert appmod.begin_shutdown(timeout=20)
    t.join(5)
    assert appmod.run_state.history[0]["exit_code"] == run_cancel.CANCELLED_EXIT and not killed
    assert "Cancelled: stopped after the current file" in "\n".join(appmod.run_state.lines)


@pytest.mark.parametrize("how", ["cancel", "closing"])
def test_cleanup_stops_between_files(tmp_path, monkeypatch, how):
    root = tmp_path / "in"
    root.mkdir()
    rows = {}
    for i in range(3):
        (root / f"m{i}.jpg").write_bytes(motion_photo_bytes())
        (root / f"m{i}.mov").write_bytes(VIDEO)
        rows[f"m{i}.jpg"] = {"file": f"m{i}.jpg", "status": "skipped", "kind": "motion_leftover",
                             "video": f"m{i}.mov", "reason": "already a motion photo"}
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(tmp_path))
    monkeypatch.setattr(appmod, "_save_preview", lambda: None)
    real = video_removal.remove_video
    removed = []

    def remove(path, *a, **k):
        removed.append(Path(path).name)
        appmod.start_closing() if how == "closing" else appmod.request_cancel()   # arrives mid-file
        return real(path, *a, **k)
    monkeypatch.setattr(appmod.video_removal, "remove_video", remove)
    appmod.run_state.cancel_requested = True                 # a stale Cancel from an earlier run...
    assert appmod.run_state.start("cleanup", reset=False)    # ...is forgotten when cleanup starts
    try:
        res = appmod.cleanup_movs(list(rows), rows, {"video_removal_mode": "move"}, root)
    finally:
        appmod.run_state.release()
    assert removed == ["m0.mov"] and res["cleaned"] == 1     # the started file went all the way
    assert res["stopped"] and res["not_processed"] == ["m1.jpg", "m2.jpg"]
    assert (root / "_originals" / "m0.mov").read_bytes() == VIDEO and not (root / "m0.mov").exists()
    assert (root / "m1.mov").read_bytes() == VIDEO and (root / "m2.mov").read_bytes() == VIDEO
    assert no_temp_left(root)
    assert "Cleanup stopped on request after 1 of 3" in appmod.run_state.lines[-1]


def test_cancel_before_converter_starts_runs_nothing(fake_app, monkeypatch):
    spawned = []
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    real_start = appmod.run_state.start

    def start(trigger, reset=True):          # Cancel arrives right after the run took the lock
        ok = real_start(trigger, reset)
        appmod.run_state.cancel_requested = True
        return ok
    monkeypatch.setattr(appmod.run_state, "start", start)
    appmod.do_run("manual")
    assert not spawned and appmod.run_state.history[0]["exit_code"] == run_cancel.CANCELLED_EXIT
    assert not appmod.run_state.running


# ─── app: one path check for every run path (F3) ────────────────────────────────

def test_saved_paths_in_excluded_dir_refuse_every_run_path(fake_app, monkeypatch):
    bad = fake_app / "AppData" / "out"
    bad.mkdir(parents=True)
    cfg = {**appmod.DEFAULT_CONFIG, "input_directory": str(fake_app / "photos"), "output_directory": str(bad),
           "schedule_enabled": True, "schedule_cron": "0 3 * * *", "watch_enabled": True}
    (fake_app / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("LIVE2MOTION_EXCLUDED_DIRS", str(fake_app / "AppData"))
    spawned = []
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(ValueError, match="Refusing to run"):
        appmod.assert_run_paths(cfg)
    for trigger in ("manual", "schedule", "watch"):
        appmod.do_run(trigger)
        assert "Refusing to run" in appmod.run_state.lines[-1]
    assert not spawned
    added = []
    monkeypatch.setattr(appmod.scheduler, "add_job", lambda *a, **k: added.append(a))
    assert "Refusing to run" in appmod.apply_schedule(cfg) and not added
    monkeypatch.setattr(appmod, "watch_observer", None)
    assert "Refusing to run" in appmod.start_watcher(cfg) and appmod.watch_observer is None
    removal = {**cfg, "output_directory": "", "video_removal_folder": str(bad)}
    with pytest.raises(ValueError):
        appmod.assert_run_paths(removal)
    ok = {**cfg, "output_directory": ""}
    appmod.assert_run_paths(ok)


# ─── small fixes ────────────────────────────────────────────────────────────────

def test_save_config_creates_missing_data_dir(tmp_path, monkeypatch):
    target = tmp_path / "new" / "data" / "config.json"
    monkeypatch.setattr(appmod, "CONFIG_FILE", target)
    appmod.save_config({"input_directory": ""})
    assert json.loads(target.read_text())["input_directory"] == ""


def test_close_controller_asks_then_waits_then_closes():
    class FakeApp:
        def __init__(self):
            self.run_state = type("S", (), {"running": True})()
            self.calls = []

        def start_closing(self):
            self.calls.append("start_closing")

        def begin_shutdown(self):
            self.calls.append("begin_shutdown")
            return True
    fa, destroyed, spawned = FakeApp(), [], []
    answers = [False, True]
    cc = desk.CloseController(fa, lambda: destroyed.append(1), ask=lambda: answers.pop(0), spawn=spawned.append)
    assert cc.on_closing() is False and fa.calls == []            # "No": keep running, nothing stopped
    assert cc.on_closing() is False and fa.calls == ["start_closing"]
    assert cc.on_closing() is False and len(spawned) == 1         # second click while stopping: no new ask
    spawned[0]()                                                  # the waiting thread
    assert fa.calls == ["start_closing", "begin_shutdown"] and destroyed == [1]
    assert cc.on_closing() is True
    idle = FakeApp()
    idle.run_state.running = False
    assert desk.CloseController(idle, lambda: None, ask=lambda: pytest.fail("asked")).on_closing() is True


def test_cancel_active_run_is_cooperative(monkeypatch):
    asked = []
    monkeypatch.setattr(appmod, "request_cancel", lambda: asked.append(1) or True)
    monkeypatch.setattr(appmod, "kill_process_tree", lambda *a, **k: pytest.fail("force kill"))
    assert desk.cancel_active_run(appmod) and asked == [1]


def test_run_motionphoto2_prints_string_exit_message(tmp_path, monkeypatch, capsys):
    mp_dir = tmp_path / "MotionPhoto2"
    mp_dir.mkdir()
    (mp_dir / "motionphoto2.py").write_text("raise SystemExit('bad things happened')\n")
    monkeypatch.setattr(desk, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    assert desk.run_motionphoto2([]) == 1
    assert "bad things happened" in capsys.readouterr().err
    (mp_dir / "motionphoto2.py").write_text("raise SystemExit(None)\n")
    assert desk.run_motionphoto2([]) == 0


def test_bind_sets_exclusive_addr_use_on_windows(monkeypatch):
    opts = []

    class FakeSock:
        def __init__(self, *a):
            pass

        def setsockopt(self, level, opt, val):
            opts.append((level, opt, val))

        def bind(self, addr):
            assert opts, "SO_EXCLUSIVEADDRUSE must be set before bind"

        def close(self):
            pass
    monkeypatch.setattr(desk.socket, "socket", FakeSock)
    monkeypatch.setattr(desk.sys, "platform", "win32")
    desk.bind_loopback_socket(47821)
    assert opts == [(socket.SOL_SOCKET, desk.SO_EXCLUSIVEADDRUSE, 1)]


def test_packaged_exclusions_never_add_a_root_relative_or_cwd_fallback(tmp_path, monkeypatch, capsys):
    import tempfile
    root = Path(tmp_path.anchor)
    env = {"LOCALAPPDATA": str(tmp_path / "L"), "TEMP": str(root), "TMP": "relative\\tmp"}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))      # tempfile fell back to cwd
    excluded = desk.packaged_excluded_dirs(env)
    assert str(root) not in excluded and "relative\\tmp" not in excluded and str(tmp_path) not in excluded
    assert str(tmp_path / "L") in excluded and str(desk.install_dir()) in excluded
    out = capsys.readouterr().out
    assert "a drive root" in out and "not an absolute path" in out and "current folder" in out
    env["TEMP"] = str(tmp_path)                              # a real TEMP that happens to be cwd: kept
    assert str(tmp_path) in desk.packaged_excluded_dirs(env)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink needs privileges on Windows")
def test_symlinked_files_into_an_excluded_dir_are_left_alone(tmp_path, monkeypatch, capsys):
    secret = tmp_path / "AppData"
    secret.mkdir()
    (secret / "s.jpg").write_bytes(JPEG)
    (secret / "s.mov").write_bytes(VIDEO)
    home = tmp_path / "home"
    make_pairs(home, 1)
    (home / "link.jpg").symlink_to(secret / "s.jpg")
    (home / "link.mov").symlink_to(secret / "s.mov")
    monkeypatch.setenv("LIVE2MOTION_EXCLUDED_DIRS", str(secret))
    code, out, rows = run_engine(home, monkeypatch, capsys, "--overwrite", "--delete-video",
                                 "--video-removal-mode", "move")
    by = {r["file"]: r for r in rows}
    assert by["link.jpg"]["status"] == "skipped" and "never uses" in by["link.jpg"]["reason"]
    assert by["p0.jpg"]["status"] == "converted"
    assert (home / "link.jpg").is_symlink() and (home / "link.mov").is_symlink()
    assert (secret / "s.jpg").read_bytes() == JPEG and (secret / "s.mov").read_bytes() == VIDEO
    assert extract._collect_images(home.resolve(), False) == ["p0.jpg"]
    with pytest.raises(extract.ExtractError, match="never uses"):
        extract.run_extract(None, str(home / "link.jpg"), None, False, dry_run=True)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-image", str(home / "link.jpg"),
                                      "--input-video", str(home / "p0.mov"), "--dry-run"])
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    with pytest.raises(SystemExit) as e:
        mp.main()
    assert e.value.code == 2 and "Refusing" in capsys.readouterr().out


def test_packaged_exclusions_cover_temp_and_install_dir(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "L"), "APPDATA": str(tmp_path / "R"), "TEMP": str(tmp_path / "T")}
    desk.configure_environment(tmp_path, "tok", packaged=True, env=env)
    excluded = env["LIVE2MOTION_EXCLUDED_DIRS"].split(os.pathsep)
    assert str(tmp_path / "T") in excluded and str(desk.install_dir()) in excluded
    import tempfile
    assert tempfile.gettempdir() in excluded and str(tmp_path / "L") in excluded
