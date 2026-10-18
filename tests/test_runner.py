import sys
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
