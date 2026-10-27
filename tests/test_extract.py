import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

import extract  # noqa: E402

pytest.importorskip("lxml")


# ── synthetic files ────────────────────────────────────────────────────────────

def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def fake_mp4(brand=b"isom", extra=b"moov") -> bytes:
    ftyp = box(b"ftyp", brand + b"\0\0\2\0" + brand)
    return ftyp + box(extra, b"\0" * 40) + box(b"mdat", bytes(range(256)) * 4)


def jpeg(xmp: str = "") -> bytes:
    body = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    if xmp:
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode()
        body += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return body + b"\xff\xdb\x00\x04\x00\x00" + b"\x12\x34" * 50 + b"\xff\xd9"


V1_XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
          '<rdf:Description xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
          'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
          'xmlns:Item="http://ns.google.com/photos/1.0/container/item/" GCamera:MotionPhoto="1">'
          '<Container:Directory><rdf:Seq>'
          '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Length="0"/></rdf:li>'
          '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="%d"/></rdf:li>'
          '</rdf:Seq></Container:Directory></rdf:Description></rdf:RDF></x:xmpmeta>')

MICRO_XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
             '<rdf:Description xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
             'GCamera:MicroVideo="1" GCamera:MicroVideoVersion="1" GCamera:MicroVideoOffset="%d"/>'
             '</rdf:RDF></x:xmpmeta>')


def v1_photo(video: bytes, length=None):
    return jpeg(V1_XMP % (len(video) if length is None else length)) + video


def micro_photo(video: bytes, offset=None):
    return jpeg(MICRO_XMP % (len(video) if offset is None else offset)) + video


def samsung_photo(video: bytes, entry_offset=None):
    name = extract.SAMSUNG_MARKER
    tag = b"\x00\x00\x30\x0a" + struct.pack("<i", len(name)) + name + video
    sefh = b"SEFH" + struct.pack("<ii", 107, 1) + b"\x00\x00\x30\x0a" \
        + struct.pack("<ii", len(tag) if entry_offset is None else entry_offset, len(tag))
    return jpeg() + tag + sefh + struct.pack("<i", len(sefh)) + b"SEFT"


FORMATS = {"v1": v1_photo, "micro": micro_photo, "samsung": samsung_photo}
BROKEN = {"v1": lambda v: v1_photo(v, length=len(v) - 7),
          "micro": lambda v: micro_photo(v, offset=len(v) + 5000),
          "samsung": lambda v: samsung_photo(v, entry_offset=3)}


@pytest.fixture
def video():
    return fake_mp4()


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ── locating ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", FORMATS)
def test_locates_video_from_metadata(tmp_path, video, fmt):
    p = write(tmp_path, "a.jpg", FORMATS[fmt](video))
    data = p.read_bytes()
    loc = extract.locate_video(data, str(p))
    assert loc is not None and loc.method != "scan for ftyp"
    assert data[loc.start:loc.end] == video


@pytest.mark.parametrize("fmt", FORMATS)
def test_wrong_offset_falls_back_to_ftyp_scan(tmp_path, video, fmt):
    p = write(tmp_path, "a.jpg", BROKEN[fmt](video))
    data = p.read_bytes()
    loc = extract.locate_video(data, str(p))
    assert loc is not None and loc.method == "scan for ftyp"
    assert data[loc.start:loc.end] == video


def test_plain_jpeg_is_not_a_motion_photo(tmp_path):
    p = write(tmp_path, "a.jpg", jpeg())
    assert extract.locate_video(p.read_bytes(), str(p)) is None
    with pytest.raises(extract.ExtractError):
        extract.extract_one(str(p), str(tmp_path / "out"))


def test_scan_skips_the_images_own_heic_ftyp(tmp_path, video):
    heic = box(b"ftyp", b"heic\0\0\0\0mif1heic") + box(b"meta", b"\0" * 64)
    assert extract.locate_video(heic + video, None) is None   # no motion photo flag: left alone
    flagged = heic + b'<x:xmpmeta GCamera:MotionPhoto="1"></x:xmpmeta>' + video
    loc = extract.locate_video(flagged, None)
    assert loc and flagged[loc.start:loc.end] == video


# ── extraction ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", FORMATS)
def test_extract_writes_mp4_and_keeps_original(tmp_path, video, fmt):
    data = FORMATS[fmt](video)
    p = write(tmp_path, "IMG_1.jpg", data)
    res = extract.extract_one(str(p), str(tmp_path))
    out = Path(res["outputs"][0])
    assert out == tmp_path / "IMG_1.mp4" and out.read_bytes() == video
    assert p.read_bytes() == data
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".mp2tmp")]


def test_never_overwrites_existing_files(tmp_path, video):
    p = write(tmp_path, "IMG_1.jpg", v1_photo(video))
    existing = write(tmp_path, "IMG_1.mp4", b"precious")
    first = extract.extract_one(str(p), str(tmp_path))["outputs"][0]
    second = extract.extract_one(str(p), str(tmp_path))["outputs"][0]
    assert existing.read_bytes() == b"precious"
    assert Path(first).name == "IMG_1_1.mp4" and Path(second).name == "IMG_1_2.mp4"


def test_output_directory_is_created(tmp_path, video):
    p = write(tmp_path, "IMG_1.jpg", micro_photo(video))
    res = extract.extract_one(str(p), str(tmp_path / "new" / "dir"))
    assert Path(res["outputs"][0]).read_bytes() == video


def test_dry_run_writes_nothing(tmp_path, video):
    p = write(tmp_path, "IMG_1.jpg", samsung_photo(video))
    res = extract.extract_one(str(p), str(tmp_path / "out"), make_mov=True, strip_still=True, dry_run=True)
    assert [Path(o).name for o in res["outputs"]] == ["IMG_1.mov", "IMG_1_still.jpg"]
    assert sorted(os.listdir(tmp_path)) == ["IMG_1.jpg"]


def test_mov_only_when_container_is_safe(tmp_path):
    safe = write(tmp_path, "a.jpg", v1_photo(fake_mp4(b"isom")))
    assert Path(extract.extract_one(str(safe), str(tmp_path / "o1"), make_mov=True)["outputs"][0]).suffix == ".mov"
    fragmented = write(tmp_path, "b.jpg", v1_photo(fake_mp4(b"isom", extra=b"moof")))
    res = extract.extract_one(str(fragmented), str(tmp_path / "o2"), make_mov=True)
    assert Path(res["outputs"][0]).suffix == ".mp4" and any("not safe" in n for n in res["notes"])
    gpp = write(tmp_path, "c.jpg", v1_photo(fake_mp4(b"3gp4")))
    assert Path(extract.extract_one(str(gpp), str(tmp_path / "o3"), make_mov=True)["outputs"][0]).suffix == ".mp4"


@pytest.mark.parametrize("fmt", FORMATS)
def test_stripped_still_is_the_bare_jpeg(tmp_path, video, fmt):
    p = write(tmp_path, "IMG_1.jpg", FORMATS[fmt](video))
    res = extract.extract_one(str(p), str(tmp_path / "out"), strip_still=True, et=None)
    still = Path(res["outputs"][1])
    assert still.name == "IMG_1_still.jpg"
    data = still.read_bytes()
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9") and b"ftyp" not in data
    assert any("exiftool not found" in n for n in res["notes"])


def test_run_extract_directory_reports_each_file(tmp_path, video, capsys):
    write(tmp_path, "good.jpg", v1_photo(video))
    write(tmp_path, "plain.jpg", jpeg())
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub, "deep.jpg", micro_photo(video))
    results = []
    out = tmp_path / "out"
    counts = extract.run_extract(str(tmp_path), None, str(out), True, emit=lambda *a: results.append(a))
    assert counts == {"extracted": 2, "skipped": 1, "errors": 0}
    assert (out / "good.mp4").exists() and (out / "sub" / "deep.mp4").exists()
    assert {r[0]: r[1] for r in results} == {"good.jpg": "converted", "plain.jpg": "skipped",
                                              str(Path("sub") / "deep.jpg"): "converted"}


def test_cli_extract_emits_results(tmp_path, video):
    write(tmp_path, "good.jpg", v1_photo(video))
    proc = subprocess.run([sys.executable, str(ROOT / "MotionPhoto2" / "motionphoto2.py"), "--extract",
                           "--input-directory", str(tmp_path), "--dry-run"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    results = [json.loads(l[len("@@RESULT "):]) for l in proc.stdout.splitlines() if l.startswith("@@RESULT ")]
    assert results[0]["status"] == "planned" and results[0]["output"].replace("\\", "/").endswith("_extracted/good.mp4")
    assert not (tmp_path / "good.mp4").exists()
    real = subprocess.run([sys.executable, str(ROOT / "MotionPhoto2" / "motionphoto2.py"), "--extract",
                           "--input-image", str(tmp_path / "good.jpg"), "--extract-mov"],
                          capture_output=True, text=True, timeout=60)
    assert real.returncode == 0, real.stdout + real.stderr
    assert (tmp_path / "_extracted" / "good.mov").read_bytes() == video


def test_default_output_is_extracted_subfolder_and_rerun_skips(tmp_path, video):
    write(tmp_path, "IMG_1.jpg", v1_photo(video))
    counts = extract.run_extract(str(tmp_path), None, None, True)
    assert counts["extracted"] == 1
    assert (tmp_path / "_extracted" / "IMG_1.mp4").read_bytes() == video
    assert not (tmp_path / "IMG_1.mp4").exists()
    results = []
    counts = extract.run_extract(str(tmp_path), None, None, True, emit=lambda *a: results.append(a))
    assert counts == {"extracted": 0, "skipped": 1, "errors": 0}
    assert results[0][2] == "already extracted"
    assert sorted(os.listdir(tmp_path / "_extracted")) == ["IMG_1.mp4"]
    counts = extract.run_extract(str(tmp_path), None, None, True, force=True)
    assert counts["extracted"] == 1 and (tmp_path / "_extracted" / "IMG_1_1.mp4").exists()


def test_converter_ignores_extracted_folder(tmp_path, video):
    pytest.importorskip("exiftool")
    write(tmp_path, "IMG_1.jpg", v1_photo(video))
    extract.run_extract(str(tmp_path), None, None, True)
    # a stray photo + same-name video inside _extracted/ must not be paired
    write(tmp_path / "_extracted", "IMG_1.jpg", jpeg())
    proc = subprocess.run([sys.executable, str(ROOT / "MotionPhoto2" / "motionphoto2.py"),
                           "--input-directory", str(tmp_path), "--recursive", "--dry-run"],
                          capture_output=True, text=True, timeout=120)
    assert "_extracted" not in proc.stdout.replace(str(tmp_path), "")
