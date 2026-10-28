import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("httpx")
import app as appmod  # noqa: E402


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_kill_process_tree_takes_down_grandchildren(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"open(r'{pidfile}','w').write(str(c.pid));time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    appmod.kill_process_tree(proc, grace=2)
    proc.wait(timeout=5)
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)   # reaped by init/our group kill; must not exist


def test_run_log_is_bounded_and_readable_by_absolute_index(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "MAX_LOG_LINES", 5)
    st = appmod.RunState.__new__(appmod.RunState)
    appmod.RunState.__init__(st)
    from collections import deque
    st.lines = deque(maxlen=5)
    assert st.start("manual")
    for i in range(12):
        st.emit(f"line {i}")
    items, nxt, _ = st.read_from(0)
    assert nxt == 12 and [i for i, _ in items] == [7, 8, 9, 10, 11]
    assert items[-1][1].endswith("line 11")
    items, nxt, _ = st.read_from(11)
    assert [i for i, _ in items] == [11]


def test_run_log_file(monkeypatch, tmp_path):
    log = tmp_path / "run.log"
    monkeypatch.setenv("LIVE2MOTION_LOG_FILE", str(log))
    st = appmod.RunState()
    st.start("manual")
    st.emit("hello")
    assert "hello" in log.read_text(encoding="utf-8")


def test_popen_decodes_utf8_filenames(monkeypatch, tmp_path):
    out = tmp_path / "o.py"
    out.write_text("import sys;sys.stdout.buffer.write('caf\\u00e9 \\u65e5\\u672c\\n'.encode('utf-8')+b'\\xff\\n')")
    monkeypatch.setattr(appmod, "BIN", [sys.executable, str(out)])
    monkeypatch.setattr(appmod, "load_config", lambda: {**appmod.DEFAULT_CONFIG, "input_directory": ""})
    st = appmod.RunState()
    monkeypatch.setattr(appmod, "run_state", st)
    appmod.do_run("test")
    assert any("café 日本" in l for l in st.lines)
    assert st.history and st.history[0]["exit_code"] == 0


def test_result_lines_feed_results_table_not_log():
    st = appmod.RunState()
    st.start("manual")
    st.emit('@@RESULT {"file": "a<b>.heic", "status": "converted", "reason": "", "output": "/o/a.heic"}')
    st.emit('@@RESULT {"file": "b.heic", "status": "weird", "reason": "x"}')
    st.emit("@@RESULT not json")
    st.emit("normal line")
    items, total = st.read_results(0)
    assert total == 2 and items[0][1]["file"] == "a<b>.heic"
    assert items[1][1]["status"] == "error"          # unknown statuses are coerced
    assert len(st.lines) == 1 and st.stats["converted"] == 1    # only the real "converted" result counts


def test_converted_count_ignores_photos_that_failed_verification():
    st = appmod.RunState()
    st.start("manual")
    for n in range(3):
        st.emit(f"[INFO] Writing output file: /in/{n}.jpg")      # logged before verification
    st.emit('@@RESULT {"file": "0.jpg", "status": "converted", "output": "/in/0.jpg"}')
    st.emit('@@RESULT {"file": "1.jpg", "status": "error", "reason": "Original kept, merged output failed verification"}')
    st.emit('@@RESULT {"file": "2.jpg", "status": "skipped", "kind": "conflict"}')
    assert st.stats["converted"] == 1


def test_results_endpoint_and_stream(monkeypatch):
    from fastapi.testclient import TestClient
    st = appmod.RunState()
    monkeypatch.setattr(appmod, "run_state", st)
    st.start("manual")
    st.emit('@@RESULT {"file": "a.heic", "status": "skipped", "reason": "no matching video", "output": ""}')
    st.finish(0)
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    assert c.get("/api/results").json()["results"][0]["reason"] == "no matching video"
