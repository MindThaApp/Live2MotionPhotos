import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("httpx")
import app as appmod  # noqa: E402


@pytest.mark.parametrize("name,temp", [
    ("IMG_1.HEIC", False), ("IMG_1.mov", False),
    (".IMG_1.HEIC.icloud", True), ("IMG_1.HEIC.icloud", True), ("~$IMG_1.heic", True),
    ("IMG_1.heic.tmp", True), ("IMG_1.jpg.part", True), ("IMG_1.XMP.heic", True),
    (".mp2tmp.abc.part", True),
])
def test_is_temp_name(name, temp):
    assert appmod.is_temp_name(name) is temp


def make(tmp_path, ready, stable=0.3):
    return appmod.MediaHandler(10, None, stable_seconds=stable, poll=0.05, on_ready=lambda: ready.append(1))


def ev(path, directory=False):
    return SimpleNamespace(src_path=str(path), dest_path=str(path), is_directory=directory)


def test_events_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.run_state, "running", False)
    h = make(tmp_path, [])
    h._reset = lambda: None
    h.on_created(ev(tmp_path / "a.heic"))
    h.on_modified(ev(tmp_path / "b.jpg"))
    h.on_created(ev(tmp_path / ".c.heic.icloud"))
    h.on_created(ev(tmp_path / "notes.txt"))
    h.on_modified(ev(tmp_path, directory=True))
    assert h._pending == {str(tmp_path / "a.heic"), str(tmp_path / "b.jpg")}


def test_events_ignored_while_run_active(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.run_state, "running", True)
    h = make(tmp_path, [])
    h._reset = lambda: None
    h.on_modified(ev(tmp_path / "a.heic"))
    assert not h._pending


def test_waits_for_growing_file_then_ready(tmp_path):
    f = tmp_path / "a.heic"
    f.write_bytes(b"x")
    h = make(tmp_path, [], stable=0.4)
    h._pending.add(str(f))
    import threading
    def grow():
        for i in range(4):
            time.sleep(0.15)
            with open(f, "ab") as fh:
                fh.write(b"y" * 10)
    t = threading.Thread(target=grow); t.start()
    start = time.monotonic()
    assert h.wait_until_stable() is True
    t.join()
    assert time.monotonic() - start >= 0.6 + 0.3   # growth ended ~0.6s, plus the 0.4s stable window
    assert not h._pending


def test_vanished_file_not_ready(tmp_path):
    h = make(tmp_path, [])
    h._pending.add(str(tmp_path / "gone.heic"))
    assert h.wait_until_stable() is False
