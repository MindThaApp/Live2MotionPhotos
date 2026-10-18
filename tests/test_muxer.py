import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")

from lxml import etree  # noqa: E402

import utils  # noqa: E402
from Muxer import Muxer  # noqa: E402
import constants as const  # noqa: E402

# Minimal JPEG: SOI, APP0, EOI
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


class StubExif:
    """Only get_metadata is needed on the no_xmp path."""
    def get_metadata(self, files):
        return [{"File:FileTypeExtension": "jpg"}, {"File:FileTypeExtension": "mp4"}]


def _make(tmp_path, **kw):
    img = tmp_path / "a.jpg"
    vid = tmp_path / "a.mp4"
    img.write_bytes(JPEG)
    vid.write_bytes(MP4)
    return img, vid, Muxer(str(img), str(vid), StubExif(), **kw)


def test_merge_xmp_keeps_all_elements(tmp_path):
    _, _, m = _make(tmp_path)
    src = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Attr="v">'
        + "".join(f"<xmp:Tag{i}>{i}</xmp:Tag{i}>" for i in range(6))
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    m.merge_xmp(src.encode())
    desc = m.xmp.find(".//rdf:Description", const.NAMESPACES)
    names = {etree.QName(c).localname for c in desc}
    assert {f"Tag{i}" for i in range(6)} <= names


def test_merge_xmp_bad_input_logged_not_raised(tmp_path, caplog):
    _, _, m = _make(tmp_path)
    m.merge_xmp(b"<not xml")
    assert any("Could not copy" in r.message for r in caplog.records)


@pytest.mark.parametrize("delete_video", [False, True])
def test_no_xmp_does_not_crash(tmp_path, delete_video):
    img, vid, m = _make(tmp_path, no_xmp=True, delete_video=delete_video)
    m.mux()
    out = Path(m.output_fpath)
    assert out.exists() and out.read_bytes().startswith(JPEG)
    assert vid.exists() is (not delete_video)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".mp2tmp")]


def test_atomic_write_ok(tmp_path):
    t = tmp_path / "out.bin"
    t.write_bytes(b"old")
    utils.atomic_write(str(t), b"new")
    assert t.read_bytes() == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    t = tmp_path / "out.bin"
    t.write_bytes(b"original")

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        utils.atomic_write(str(t), b"new data")
    assert t.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]
