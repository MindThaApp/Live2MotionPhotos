import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("httpx")
import app as appmod  # noqa: E402


def test_build_cmd_dry_run_flag():
    assert "--dry-run" in appmod.build_cmd({**appmod.DEFAULT_CONFIG, "dry_run": True})
    assert "--dry-run" not in appmod.build_cmd(appmod.DEFAULT_CONFIG)
    assert appmod.DEFAULT_CONFIG["dry_run"] is False


def test_dry_run_lines_counted_as_planned_conversions():
    st = appmod.RunState()
    st.start("manual")
    st.emit("[DRY-RUN] Would convert a.heic + a.mov -> a.LIVE.heic")
    st.emit("[DRY-RUN] Would remove video a.mov (trash)")
    assert st.stats["converted"] == 1 and st.stats["errors"] == 0


def test_run_endpoint_passes_override(monkeypatch):
    from fastapi.testclient import TestClient
    seen = []
    monkeypatch.setattr(appmod, "do_run", lambda trigger="manual", dry_run=None: seen.append(dry_run))
    monkeypatch.setattr(appmod.run_state, "running", False)
    c = TestClient(appmod.app, base_url="http://localhost:7000")
    assert c.post("/api/run?dry_run=true").status_code == 200
    assert c.post("/api/run").status_code == 200
    import time; time.sleep(0.2)
    assert sorted(seen, key=str) == [None, True]


def test_report_dry_run_output(capsys, tmp_path):
    pytest.importorskip("exiftool")
    import motionphoto2 as mp
    args = SimpleNamespace(overwrite=False, delete_video=True)
    kw = {"video_removal_mode": "trash"}
    mp.report_dry_run(args, "a.heic", "/in/a.heic", "/in/a.mov", None, kw)
    mp.report_dry_run(args, "a.heic", "/in/a.heic", "/in/a.mov", "/out", kw)
    out = capsys.readouterr().out
    assert "[DRY-RUN] Would convert a.heic + /in/a.mov -> /in/a.LIVE.heic" in out
    assert "-> /out/a.heic" in out
    assert "[DRY-RUN] Would remove video /in/a.mov (trash)" in out
    args.overwrite = True
    assert mp.planned_output(args, "/in/a.heic", None) == "/in/a.heic"
