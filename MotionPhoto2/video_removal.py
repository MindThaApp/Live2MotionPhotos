"""Safe removal of source videos after muxing (shared by Muxer.py and app.py)."""

import hashlib
import logging
import os
import re
import shutil
import struct

from pathlib import Path

logger = logging.getLogger("video_removal")

MODES = ("trash", "move", "delete")
VIDEO_EXTS = {".mov", ".mp4"}
DEFAULT_FOLDER_NAME = "_deleted_videos"


def in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("LIVE2MOTION_DOCKER") == "1"


def default_mode() -> str:
    # No recycle bin inside a container, so keep the files in a folder instead
    return "move" if in_docker() else "trash"


def normalize_mode(mode) -> str:
    mode = str(mode or "").strip().lower()
    return mode if mode in MODES else default_mode()


def removal_folder(root, folder=None) -> Path:
    """Folder used by "move" mode: `folder` if given (relative paths are taken
    relative to root), otherwise <root>/_deleted_videos."""
    root = Path(root).resolve()
    if not folder:
        return root / DEFAULT_FOLDER_NAME
    folder = Path(folder)
    return (folder if folder.is_absolute() else root / folder).resolve()


def is_inside(path, parent) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def verify_muxed_output(output_path, video_path, video_offset: int, image_size: int) -> str:
    """Return "" if the muxed file looks complete, else the reason it does not.
    video_offset is where the source video bytes start inside the output."""
    if not os.path.isfile(output_path):
        return f"output file does not exist: {output_path}"
    video_size = os.path.getsize(video_path)
    out_size = os.path.getsize(output_path)
    if out_size < image_size + video_size:
        return f"output is {out_size} bytes, expected at least {image_size + video_size}"
    if video_offset < 0 or video_offset + video_size > out_size:
        return f"video offset {video_offset} + {video_size} bytes is outside the output ({out_size} bytes)"
    with open(output_path, "rb") as out, open(video_path, "rb") as vid:
        out.seek(video_offset)
        while True:
            chunk = vid.read(1024 * 1024)
            if not chunk:
                return ""
            if out.read(len(chunk)) != chunk:
                return "embedded video bytes differ from the source video"


SAMSUNG_MOTION_PHOTO_DATA_ID = bytes([0x00, 0x00, 0x30, 0x0a])
VIDEO_SIGNATURES = (b"ftyp", b"wide")
XMP_START, XMP_END = b"<x:xmpmeta", b"</x:xmpmeta>"


def _le32(b: bytes) -> int:
    return struct.unpack("<i", b)[0]


def _samsung_video_location(f, size: int):
    """(offset, length) of the video from the Samsung SEF trailer, or None.
    Reads only the last few hundred bytes of the file, never the video."""
    if size < 24:
        return None
    f.seek(size - 8)
    tail = f.read(8)
    if tail[4:] != b"SEFT":
        return None
    sefh_len = _le32(tail[:4])
    sefh_start = size - 8 - sefh_len
    if sefh_len < 12 or sefh_start < 0:
        return None
    f.seek(sefh_start)
    sefh = f.read(sefh_len)
    if sefh[:4] != b"SEFH":
        return None
    for i in range(_le32(sefh[8:12])):
        entry = sefh[12 + 12 * i:24 + 12 * i]
        if len(entry) < 12 or entry[:4] != SAMSUNG_MOTION_PHOTO_DATA_ID:
            continue
        tag_start = sefh_start - _le32(entry[4:8])    # offsets count back from the SEFH block
        tag_len = _le32(entry[8:12])
        f.seek(tag_start)
        head = f.read(8)
        if tag_start < 0 or len(head) < 8:
            return None
        data_start = tag_start + 8 + _le32(head[4:8])  # skip tag id, name length and name
        data_len = tag_len - (data_start - tag_start)
        f.seek(data_start)
        data = f.read(12) if data_len == 12 else b""
        if data[:4] == b"mpv2":                        # HEIC: pointer to the video in an mpvd box
            return struct.unpack(">i", data[4:8])[0], struct.unpack(">i", data[8:12])[0]
        return data_start, data_len
    return None


def _xmp_video_location(f, size: int):
    """(offset, length) from the Container:Directory in the XMP packet, or None.
    The Motion Photo video is the last item, so it starts `length` bytes before the end."""
    f.seek(0)
    data = f.read()
    start = data.find(XMP_START)
    end = data.find(XMP_END, start) if start != -1 else -1
    if end == -1:
        return None
    try:
        from lxml import etree
        root = etree.fromstring(data[start:end + len(XMP_END)])
    except Exception:
        return None
    ns = {"Item": "http://ns.google.com/photos/1.0/container/item/",
          "Container": "http://ns.google.com/photos/1.0/container/"}
    for item in root.iter("{%s}Item" % ns["Container"]):
        if item.get("{%s}Semantic" % ns["Item"]) == "MotionPhoto":
            try:
                length = int(item.get("{%s}Length" % ns["Item"]))
            except (TypeError, ValueError):
                return None
            return size - length, length
    return None


def find_embedded_video(path, use_xmp: bool = True):
    """(offset, length) of the video embedded in a Motion Photo, or None.
    Checks the Samsung trailer first (tail bytes only), then, if use_xmp, the XMP."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            loc = _samsung_video_location(f, size)
            if loc is None and use_xmp:
                loc = _xmp_video_location(f, size)
            if loc is None or loc[0] < 0 or loc[1] <= 8 or loc[0] + loc[1] > size:
                return None
            f.seek(loc[0])
            if not any(sig in f.read(15) for sig in VIDEO_SIGNATURES):
                return None
            return loc
    except (OSError, struct.error):
        return None


def _sha256(path, offset=0, length=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(offset)
        left = length
        while left is None or left > 0:
            chunk = f.read(1024 * 1024 if left is None else min(1024 * 1024, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def verify_embedded_video(photo_path, video_path) -> str:
    """Return "" if the video inside the Motion Photo is identical (size and
    SHA-256) to the standalone video, else the reason it is not. Used before
    a standalone video is offered for cleanup."""
    loc = find_embedded_video(photo_path)
    if loc is None:
        return "no embedded video found in the photo"
    offset, length = loc
    video_size = os.path.getsize(video_path)
    if length != video_size:
        return f"embedded video is {length} bytes, standalone video is {video_size}"
    problem = verify_muxed_output(photo_path, video_path, offset, offset)
    if problem:
        return problem
    if _sha256(photo_path, offset, length) != _sha256(video_path):
        return "embedded video hash differs from the standalone video"
    return ""


def _unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 1
    while True:
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _move(path: Path, root: Path, folder) -> Path:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    dest = _unique_path(removal_folder(root, folder) / rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    return dest


def remove_video(path, mode=None, root=None, folder=None) -> str:
    """The only place video files get removed. Returns the action taken
    ("trash", "move" or "delete"). Raises ValueError for non-video files or
    paths outside root. If root is None, the video's own folder is the root."""
    p = Path(path).resolve()
    if p.suffix.lower() not in VIDEO_EXTS:
        raise ValueError(f"not a video file: {path}")
    if root is not None and not is_inside(p, root):
        raise ValueError(f"path is outside the configured root: {path}")
    if not p.is_file():
        raise FileNotFoundError(f"video file doesn't exist: {path}")
    r = Path(root).resolve() if root is not None else p.parent

    mode = normalize_mode(mode)
    if mode == "trash":
        try:
            from send2trash import send2trash
            send2trash(str(p))
            logger.info("Moved to trash: %s", p)
            return "trash"
        except Exception as e:
            logger.warning("Could not use the trash (%s), moving %s to the removal folder instead", e, p)
            mode = "move"
    if mode == "move":
        dest = _move(p, r, folder)
        logger.info("Moved %s to %s", p, dest)
        return "move"
    p.unlink()
    logger.info("Permanently deleted: %s", p)
    return "delete"
