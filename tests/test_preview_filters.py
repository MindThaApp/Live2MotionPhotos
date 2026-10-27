import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")
pytest.importorskip("httpx")

import app as appmod  # noqa: E402
import motionphoto2 as mp  # noqa: E402
from SamsungTags import SamsungTags  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
VIDEO = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  " + bytes(range(256)) * 20


def motion_photo_bytes(video=VIDEO) -> bytes:
    img = JPEG + b"\x00" * 100
    tags = SamsungTags(video, "jpeg")
    tags.set_image_size(len(img))
    return img + tags.video_footer()


class FakeET:
    """Stands in for exiftool: no XMP flags, content ids from a name -> id map."""
    ids: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_metadata(self, files):
        out = []
        for f in files:
            cid = self.ids.get(Path(f).name)
            out.append({"MakerNotes:ContentIdentifier": cid, "QuickTime:ContentIdentifier": cid} if cid else {})
        return out

    def execute(self, *a, **k):
        return b"" if k.get("raw_bytes") else ""


def scan(root, monkeypatch, capsys, *extra):
    monkeypatch.setattr(mp.exiftool, "ExifToolHelper", FakeET)
    monkeypatch.setattr(sys, "argv", ["mp", "--input-directory", str(root), "--dry-run", "--recursive", *extra])
    monkeypatch.setenv("HOME", str(root.parent))
    try:
        mp.main()
    except SystemExit:
        pass
    rows = {}
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("@@RESULT "):
            r = json.loads(line[9:])
            rows[r["file"].replace("\\", "/")] = r
    return rows


def build_fixture(root):
    root.mkdir()
    (root / "pair.jpg").write_bytes(JPEG + b"\x00" * 50)
    (root / "pair.mov").write_bytes(b"pairmov")
    (root / "muxed.jpg").write_bytes(motion_photo_bytes())
    (root / "muxed.mov").write_bytes(VIDEO)               # leftover MOV
    (root / "solo_muxed.jpg").write_bytes(motion_photo_bytes())
    (root / "lone.jpg").write_bytes(JPEG + b"\x00" * 50)
    (root / "orphan.mov").write_bytes(b"orphan")


def test_classification_by_name(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    build_fixture(root)
    rows = scan(root, monkeypatch, capsys)
    kinds = {k: v.get("kind") for k, v in rows.items()}
    assert kinds == {"pair.jpg": "ready", "muxed.jpg": "motion_leftover", "solo_muxed.jpg": "motion_only",
                     "lone.jpg": "no_mov", "orphan.mov": "no_photo"}
    assert rows["muxed.jpg"]["video"] == "muxed.mov"
    assert "pair.mov" not in rows and "muxed.mov" not in rows     # paired MOVs are not "no photo"


def test_classification_by_exif_match(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    build_fixture(root)
    FakeET.ids = {"pair.jpg": "A", "pair.mov": "A", "muxed.jpg": "B", "muxed.mov": "B", "orphan.mov": "Z"}
    try:
        rows = scan(root, monkeypatch, capsys, "--exif-match")
    finally:
        FakeET.ids = {}
    kinds = {k: v.get("kind") for k, v in rows.items()}
    assert kinds["pair.jpg"] == "ready" and kinds["muxed.jpg"] == "motion_leftover"
    assert kinds["solo_muxed.jpg"] == "motion_only" and kinds["lone.jpg"] == "no_mov"
    assert kinds["orphan.mov"] == "no_photo" and "muxed.mov" not in kinds


def test_unreadable_photo_is_error_row(tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    build_fixture(root)

    def boom(path, et):
        raise OSError("cannot read")
    monkeypatch.setattr(mp, "is_motion_photo", boom)
    rows = scan(root, monkeypatch, capsys)
    assert rows["pair.jpg"]["kind"] == "error" and rows["pair.jpg"]["status"] == "error"


# ── app side ───────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(appmod, "IN_DOCKER", False)
    monkeypatch.setenv("LIVE2MOTION_BROWSE_ROOTS", str(tmp_path))
    monkeypatch.delenv("LIVE2MOTION_TOKEN", raising=False)
    root = tmp_path / "in"
    build_fixture(root)
    cfg = {**appmod.DEFAULT_CONFIG, "input_directory": str(root), "video_removal_mode": "move"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(appmod, "last_preview", {})
    rows = [
        {"file": "pair.jpg", "status": "planned", "kind": "ready"},
        {"file": "muxed.jpg", "status": "skipped", "kind": "motion_leftover", "video": "muxed.mov"},
        {"file": "solo_muxed.jpg", "status": "skipped", "kind": "motion_only"},
        {"file": "lone.jpg", "status": "skipped", "kind": "no_mov"},
        {"file": "orphan.mov", "status": "skipped", "kind": "no_photo"},
    ]
    appmod.set_preview(appmod.preview_fingerprint(cfg), rows)
    from fastapi.testclient import TestClient
    return root, cfg, TestClient(appmod.app, base_url="http://localhost:7000")


def test_counts_api_and_persistence(env):
    _, _, c = env
    d = c.get("/api/preview").json()
    assert d["counts"] == {"all": 5, "ready": 1, "motion_leftover": 1, "motion_only": 1, "no_mov": 1, "no_photo": 1}
    assert d["count"] == 1 and "move" == d["cleanup"]["mode"]
    appmod.last_preview.clear()                          # restart: kinds survive in preview.json
    assert c.get("/api/preview").json()["counts"]["motion_leftover"] == 1


def test_old_rows_without_kind_are_derived():
    assert appmod.classify_row({"status": "planned"}) == "ready"
    assert appmod.classify_row({"status": "skipped", "reason": "no matching video"}) == "no_mov"
    assert appmod.classify_row({"status": "skipped", "reason": "already a motion photo"}) == "motion_only"
    assert appmod.classify_row({"status": "error"}) == "error"


def test_converted_counts_as_done_state(env):
    _, _, c = env
    appmod.mark_preview_done(appmod.last_preview["id"], ["pair.jpg"])
    d = c.get("/api/preview").json()
    assert d["counts"]["converted"] == 1 and "ready" not in d["counts"] and d["count"] == 0


def test_cleanup_moves_verified_mov_only(env):
    root, _, c = env
    pid = appmod.last_preview["id"]
    r = c.post("/api/cleanup-movs", json={"preview_id": pid, "files": ["muxed.jpg"]}).json()
    assert r["cleaned"] == 1 and r["details"][0]["action"] == "move"
    assert not (root / "muxed.mov").exists() and (root / "_originals" / "muxed.mov").read_bytes() == VIDEO
    assert (root / "muxed.jpg").exists()
    d = c.get("/api/preview").json()
    assert d["counts"].get("motion_leftover") is None and d["counts"]["motion_only"] == 2


def test_cleanup_refuses_when_photo_has_no_motion(env):
    root, _, c = env
    (root / "muxed.jpg").write_bytes(JPEG + b"\x00" * 50)     # photo lost its embedded video
    pid = appmod.last_preview["id"]
    r = c.post("/api/cleanup-movs", json={"preview_id": pid, "files": ["muxed.jpg"]}).json()
    assert r["cleaned"] == 0 and "no embedded video" in r["refused"][0]["reason"]
    assert (root / "muxed.mov").exists() and not (root / "_originals").exists()


def test_cleanup_refuses_when_embedded_video_differs(env):
    root, _, c = env
    (root / "muxed.mov").write_bytes(VIDEO[:-1] + b"\x01")
    r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["muxed.jpg"]}).json()
    assert r["cleaned"] == 0 and r["refused"] and (root / "muxed.mov").exists()


def test_cleanup_ignores_other_statuses(env):
    root, _, c = env
    pid = appmod.last_preview["id"]
    r = c.post("/api/cleanup-movs", json={"preview_id": pid, "files": ["pair.jpg", "lone.jpg", "orphan.mov", "solo_muxed.jpg"]}).json()
    assert r["cleaned"] == 0 and len(r["skipped"]) == 4
    assert (root / "pair.mov").exists() and (root / "orphan.mov").exists()


def test_cleanup_validation_and_stale(env):
    root, cfg, c = env
    pid = appmod.last_preview["id"]
    assert c.post("/api/cleanup-movs", json={"preview_id": pid, "files": ["../x.jpg"]}).status_code == 400
    assert c.post("/api/cleanup-movs", json={"preview_id": "nope", "files": ["muxed.jpg"]}).json()["stale"] is True
    appmod.CONFIG_FILE.write_text(json.dumps({**cfg, "video_removal_mode": "delete"}))
    r = c.post("/api/cleanup-movs", json={"preview_id": pid, "files": ["muxed.jpg"]})
    assert r.status_code == 409 and (root / "muxed.mov").exists()


def test_cleanup_disposal_modes(env, monkeypatch):
    root, cfg, c = env
    import send2trash
    sent = []
    monkeypatch.setattr(send2trash, "send2trash", lambda p: (sent.append(p), Path(p).unlink()))
    for mode, expect in (("trash", "trash"), ("delete", "delete")):
        (root / "muxed.mov").write_bytes(VIDEO)
        new = {**cfg, "video_removal_mode": mode}
        appmod.CONFIG_FILE.write_text(json.dumps(new))
        appmod.set_preview(appmod.preview_fingerprint(new), [
            {"file": "muxed.jpg", "status": "skipped", "kind": "motion_leftover", "video": "muxed.mov"}])
        r = c.post("/api/cleanup-movs", json={"preview_id": appmod.last_preview["id"], "files": ["muxed.jpg"]}).json()
        assert r["details"][0]["action"] == expect and not (root / "muxed.mov").exists()
    assert len(sent) == 1 and not (root / "_originals").exists()


def test_run_selected_marks_missing_and_ignores_non_ready(env, monkeypatch):
    root, _, c = env
    monkeypatch.setattr(appmod, "do_run", lambda *a: None)
    monkeypatch.setattr(appmod.run_state, "running", False)
    pid = appmod.last_preview["id"]
    (root / "pair.jpg").unlink()
    r = c.post("/api/run-selected", json={"preview_id": pid, "files": ["pair.jpg", "lone.jpg"]}).json()
    assert r["count"] == 0 and len(r["skipped"]) == 2
    d = c.get("/api/preview").json()
    assert d["counts"]["missing"] == 1 and "ready" not in d["counts"]
    r = c.post("/api/run-selected", json={"preview_id": pid, "files": ["pair.jpg"]}).json()    # no longer selectable
    assert r["count"] == 0 and r["skipped"][0]["reason"] == "file no longer exists"
