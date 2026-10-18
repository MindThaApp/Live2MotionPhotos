import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "MotionPhoto2"))

import video_removal as vr  # noqa: E402
from SamsungTags import SamsungTags  # noqa: E402
import constants as const  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
VIDEO = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  " + bytes(range(256)) * 20


def build(image: bytes, video: bytes, kind: str) -> bytes:
    tags = SamsungTags(video, kind)
    tags.set_image_size(len(image))
    return image + tags.video_footer()


@pytest.fixture(params=["jpeg", "heic"])
def pair(request, tmp_path):
    photo, vid = tmp_path / "a.x", tmp_path / "a.mov"
    # a fake heic: any bytes work since only the trailer is inspected
    photo.write_bytes(build(JPEG + b"\x00" * 100, VIDEO, request.param))
    vid.write_bytes(VIDEO)
    return photo, vid


def test_locates_embedded_video(pair):
    photo, vid = pair
    off, ln = vr.find_embedded_video(photo, use_xmp=False)
    assert ln == len(VIDEO) and photo.read_bytes()[off:off + ln] == VIDEO


def test_identical_video_verifies(pair):
    assert vr.verify_embedded_video(*pair) == ""


def test_different_video_rejected(pair):
    photo, vid = pair
    vid.write_bytes(VIDEO[:-1] + b"\x01")          # same size, different content
    assert "differs" in vr.verify_embedded_video(photo, vid)
    vid.write_bytes(VIDEO + b"x")                  # different size
    assert "bytes" in vr.verify_embedded_video(photo, vid)


def test_plain_photo_has_no_embedded_video(tmp_path):
    p = tmp_path / "p.jpg"
    p.write_bytes(JPEG + b"\x00" * 100)
    assert vr.find_embedded_video(p) is None


def test_xmp_only_motion_photo(tmp_path):
    xmp = const.XMP.replace('Item:Length="404"', f'Item:Length="{len(VIDEO)}"').encode()
    p = tmp_path / "g.jpg"
    p.write_bytes(JPEG + xmp + VIDEO)
    assert vr.find_embedded_video(p)[1] == len(VIDEO)
    v = tmp_path / "g.mov"
    v.write_bytes(VIDEO)
    assert vr.verify_embedded_video(p, v) == ""


class FakeET:
    def __init__(self, flags="", video=b""):
        self.calls, self.flags, self.video = [], flags, video

    def execute(self, *args, **kw):
        self.calls.append(args)
        return self.video if "-b" in args else self.flags


def test_is_motion_photo_skips_exiftool_for_trailer(pair):
    pytest.importorskip("exiftool")
    import utils
    et = FakeET()
    assert utils.is_motion_photo(str(pair[0]), et) is True
    assert et.calls == []


def test_is_motion_photo_plain_photo_never_extracts_video(tmp_path):
    pytest.importorskip("exiftool")
    import utils
    p = tmp_path / "p.jpg"
    p.write_bytes(JPEG)
    et = FakeET(flags="")
    assert utils.is_motion_photo(str(p), et) is False
    assert len(et.calls) == 1 and "-b" not in et.calls[0]


def test_is_motion_photo_flagged_photo_confirms_video(tmp_path):
    pytest.importorskip("exiftool")
    import utils
    p = tmp_path / "p.jpg"
    p.write_bytes(JPEG)
    assert utils.is_motion_photo(str(p), FakeET(flags="1", video=VIDEO)) is True
    assert utils.is_motion_photo(str(p), FakeET(flags="1", video=b"junk")) is False
