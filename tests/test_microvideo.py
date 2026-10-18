import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "MotionPhoto2"))

pytest.importorskip("exiftool")
pytest.importorskip("lxml")

from lxml import etree  # noqa: E402

import constants as const  # noqa: E402
import utils  # noqa: E402
from Muxer import Muxer  # noqa: E402
from SamsungTags import SamsungTags  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
VIDEO = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  " + bytes(range(200))


class Stub:
    def get_metadata(self, files):
        return [{}, {}]


def test_microvideo_tags_written(tmp_path):
    (tmp_path / "a.jpg").write_bytes(JPEG)
    (tmp_path / "a.mov").write_bytes(VIDEO)
    m = Muxer(str(tmp_path / "a.jpg"), str(tmp_path / "a.mov"), Stub())
    m.xmp.find(".//rdf:Description", const.NAMESPACES).set(const.GCAMER_TIMESTAMP_US, "1500000")
    m.set_microvideo_tags(1234)
    d = m.xmp.find(".//rdf:Description", const.NAMESPACES)
    assert d.get(const.GCAMERA_MICROVIDEO) == "1"
    assert d.get(const.GCAMERA_MICROVIDEO_VERSION) == "1"
    assert d.get(const.GCAMERA_MICROVIDEO_OFFSET) == "1234"
    assert d.get(const.GCAMERA_MICROVIDEO_TIMESTAMP_US) == "1500000"
    assert d.get(const.GCAMER_TIMESTAMP_US) == "1500000"   # v1 tags untouched


def test_offset_counts_back_from_end_of_file_to_video_start():
    image = JPEG + b"\x00" * 333
    tail = SamsungTags(VIDEO, "jpg")
    tail.set_image_size(len(image))
    out = image + tail.video_footer()
    offset = tail.get_video_size()          # what the muxer writes as MicroVideoOffset
    assert out[len(out) - offset:].startswith(VIDEO[:8])
    assert len(out) - offset == len(image) + tail.get_image_padding()


def test_offset_unchanged_when_image_grows():
    """XMP insertion changes the image size after the offset is computed."""
    a = SamsungTags(VIDEO, "jpg"); a.set_image_size(100)
    b = SamsungTags(VIDEO, "jpg"); b.set_image_size(100000)
    assert a.get_video_size() == b.get_video_size()


STILL = """
<Track3:SampleTime>0.0333</Track3:SampleTime>
<Track3:StillImageTransform>1 0 0 1</Track3:StillImageTransform>
<Track3:SampleTime>1.4333</Track3:SampleTime>
<Track3:StillImageTime>-1</Track3:StillImageTime>
<Track3:TrackDuration>3.0</Track3:TrackDuration>
"""


def test_still_image_time_preferred():
    assert utils.extract_still_image_time("3", STILL) == "1433300"
    assert utils.extract_track_duration("3", STILL) == "3000000"


def test_still_image_time_none_when_missing():
    assert utils.extract_still_image_time("3", "<Track3:StillImageTime>-1</Track3:StillImageTime>") is None
    assert utils.extract_still_image_time("3", "nothing") is None
