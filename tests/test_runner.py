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
