"""Safe disposal of originals after muxing (shared by Muxer.py and app.py).

One removal mode (trash | move | delete) and one folder apply to both kinds of
originals: source videos removed after muxing, and photos replaced by --overwrite.
In "move" mode both go to the same folder (default <input dir>/_originals,
keeping relative paths). The old default folder name `_deleted_videos` is still
skipped by scans so existing folders are never re-processed."""

import hashlib
import logging
import os
import shutil
import struct

from pathlib import Path
from typing import Optional

logger = logging.getLogger("video_removal")

MODES = ("trash", "move", "delete")
VIDEO_EXTS = {".mov", ".mp4"}
DEFAULT_FOLDER_NAME = "_originals"
LEGACY_FOLDER_NAMES = ("_deleted_videos",)   # previous default, still ignored by scans


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
    relative to root), otherwise <root>/_originals."""
    root = Path(root).resolve()
    if not folder:
        return root / DEFAULT_FOLDER_NAME
    folder = Path(folder)
    return (folder if folder.is_absolute() else root / folder).resolve()


def skip_folders(root, folder=None) -> list:
    """Folders scans/watchers must ignore: the removal folder plus legacy default."""
    root = Path(root).resolve()
    out = [removal_folder(root, folder)]
    out += [root / n for n in LEGACY_FOLDER_NAMES]
    return out


def is_inside(path, parent) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def jpeg_scan_tail(data) -> Optional[bytes]:
    """The bytes of a JPEG from its first SOS marker to the end (the compressed
    picture data plus anything after it), or None if the header cannot be parsed.
    Rewriting the metadata segments (XMP, EXIF) never changes these bytes."""
    if isinstance(data, (str, os.PathLike)):
        with open(data, "rb") as f:
            data = f.read()
    start = _jpeg_sos_offset(data)
    return None if start is None else bytes(data[start:])


def _jpeg_sos_offset(data) -> Optional[int]:
    """Offset of the first SOS marker, walking the segment chain from the start of
    the file; None if the chain is not a well formed JPEG header."""
    if data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:               # fill byte
            i += 1
            continue
        if marker == 0xDA:
            return i
        if marker in (0x00, 0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            return None                  # not valid before the scan
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


# ─── HEIC structure ───────────────────────────────────────────────────────────
# Rewriting the XMP of a HEIC (exiftool) moves boxes around and can make the file
# smaller, so its size says nothing. What must never change is the picture data:
# every image item (primary image, tiles, thumbnails, auxiliary images) keeps its
# bytes. Metadata items (Exif, XMP) are allowed to change.

_HEIC_META_TYPES = (b"Exif", b"mime")
_HEIC_MAX_ITEM = 512 * 1024 * 1024


def _iso_boxes(data, start: int, end: int):
    """(type, payload_start, box_end) for the boxes in data[start:end]; raises
    ValueError if they do not tile that range exactly."""
    out, i = [], start
    while i < end:
        if i + 8 > end:
            raise ValueError("truncated box header")
        size = int.from_bytes(data[i:i + 4], "big")
        kind = bytes(data[i + 4:i + 8])
        hdr = 8
        if size == 1:
            size, hdr = int.from_bytes(data[i + 8:i + 16], "big"), 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            raise ValueError("box %r runs outside the file" % kind)
        out.append((kind, i + hdr, i + size))
        i += size
    return out


def heic_image_items(source, limit: Optional[int] = None) -> dict:
    """Parse a HEIC/HEIF/AVIF file (a path or bytes, only the first `limit` bytes if
    given, which must be exactly a sequence of whole boxes). Returns
    {"primary": item_id, "items": {item_id: (type, sha256 of the item bytes)}} for all
    non-metadata items. Raises ValueError if the file is not a well formed HEIF."""
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as f:
            data = f.read() if limit is None else f.read(limit)
        if limit is not None and len(data) != limit:
            raise ValueError("file is shorter than the expected image size")
    else:
        data = bytes(source if limit is None else source[:limit])
    try:
        top = _iso_boxes(data, 0, len(data))
        if not top or top[0][0] != b"ftyp":
            raise ValueError("no ftyp box first")
        metas = [b for b in top if b[0] == b"meta"]
        if len(metas) != 1:
            raise ValueError("expected exactly one meta box")
        m_start, m_end = metas[0][1] + 4, metas[0][2]           # meta is a full box
        kids = {k: (a, b) for k, a, b in _iso_boxes(data, m_start, m_end)}
        for need in (b"pitm", b"iinf", b"iloc"):
            if need not in kids:
                raise ValueError("no %s box" % need.decode())
        a, b = kids[b"pitm"]
        primary = int.from_bytes(data[a + 4:a + 6], "big") if data[a] == 0 else int.from_bytes(data[a + 4:a + 8], "big")

        a, b = kids[b"iinf"]
        iv = data[a]
        n = int.from_bytes(data[a + 4:a + 6], "big"); pos = a + 6
        if iv != 0:
            n = int.from_bytes(data[a + 4:a + 8], "big"); pos = a + 8
        types = {}
        for kind, ia, ib in _iso_boxes(data, pos, b):
            if kind != b"infe":
                continue
            ver = data[ia]
            if ver < 2:
                raise ValueError("unsupported infe version")
            if ver == 2:
                iid, itype = int.from_bytes(data[ia + 4:ia + 6], "big"), bytes(data[ia + 8:ia + 12])
            else:
                iid, itype = int.from_bytes(data[ia + 4:ia + 8], "big"), bytes(data[ia + 10:ia + 14])
            types[iid] = itype
        if len(types) != n or primary not in types:
            raise ValueError("item list is inconsistent")

        a, b = kids[b"iloc"]
        ver = data[a]
        osz, lsz = data[a + 4] >> 4, data[a + 4] & 15
        bsz, isz = data[a + 5] >> 4, data[a + 5] & 15
        pos = a + 6
        if ver < 2:
            count, pos = int.from_bytes(data[pos:pos + 2], "big"), pos + 2
        else:
            count, pos = int.from_bytes(data[pos:pos + 4], "big"), pos + 4

        def rd(k):
            nonlocal pos
            v = int.from_bytes(data[pos:pos + k], "big")
            pos += k
            return v
        idat = kids.get(b"idat")
        items = {}
        for _ in range(count):
            iid = rd(4 if ver == 2 else 2)
            method = 0
            if ver in (1, 2):
                method = rd(2) & 15
            rd(2)                                              # data reference index
            base = rd(bsz)
            ext = []
            for _ in range(rd(2)):
                if ver in (1, 2) and isz:
                    rd(isz)
                ext.append((rd(osz), rd(lsz)))
            if pos > b:
                raise ValueError("iloc is truncated")
            if types.get(iid) in _HEIC_META_TYPES or iid not in types:
                continue
            h = hashlib.sha256()
            for off, ln in ext:
                if method == 0:
                    lo = base + off
                    if ln == 0 or lo + ln > len(data):
                        raise ValueError("item %d is outside the file" % iid)
                    h.update(data[lo:lo + ln])
                elif method == 1 and idat:
                    lo = idat[0] + base + off
                    if lo + ln > idat[1]:
                        raise ValueError("item %d is outside idat" % iid)
                    h.update(data[lo:lo + ln])
                else:
                    raise ValueError("unsupported item location")
            items[iid] = (types[iid], h.hexdigest())
        if primary not in items:
            raise ValueError("primary image has no data")
        return {"primary": primary, "items": items}
    except (IndexError, struct.error) as e:
        raise ValueError("malformed HEIF structure: %s" % e)


def _compare_heic(original: dict, merged: dict) -> str:
    if original["primary"] != merged["primary"]:
        return "primary image changed in the merged photo"
    if set(original["items"]) != set(merged["items"]):
        return "image items of the merged photo differ from the original"
    for iid, want in original["items"].items():
        if merged["items"][iid] != want:
            return "picture data of the merged photo differs from the original (item %d)" % iid
    return ""


def verify_muxed_output(output_path, video_path, video_offset: int, image_size: int,
                        image_scan: Optional[bytes] = None, image_end: Optional[int] = None,
                        heic_items: Optional[dict] = None) -> str:
    """Return "" if the muxed file looks complete, else the reason it does not.
    video_offset is where the source video bytes start inside the output.

    Without image_scan the output must be at least as large as the original photo
    plus the video. That is wrong for JPEGs: rewriting the XMP can legitimately make
    the photo smaller. For a JPEG, pass image_scan (jpeg_scan_tail of the original)
    and image_end (where the merged photo ends in the output): the output must then
    start with a well formed JPEG header whose scan data is byte for byte the
    original's and ends at image_end, followed by the unchanged video.

    For a HEIC, pass heic_items (heic_image_items of the original) and image_end: the
    first image_end bytes of the output must be well formed boxes whose image items
    (primary, tiles, thumbnails) are byte for byte the original's. The size rule is
    the fallback when the original could not be parsed (heic_items None)."""
    if not os.path.isfile(output_path):
        return f"output file does not exist: {output_path}"
    video_size = os.path.getsize(video_path)
    out_size = os.path.getsize(output_path)
    if heic_items is not None and image_end is not None:
        if image_end > video_offset or image_end > out_size:
            return f"merged photo end {image_end} is inconsistent with the video offset {video_offset}"
        try:
            merged = heic_image_items(output_path, limit=image_end)
        except (ValueError, OSError) as e:
            return f"merged photo is not a well formed HEIC: {e}"
        problem = _compare_heic(heic_items, merged)
        if problem:
            return problem
    elif image_scan is None or image_end is None:
        if out_size < image_size + video_size:
            return f"output is {out_size} bytes, expected at least {image_size + video_size}"
    else:
        if not image_scan or image_end > video_offset or image_end > out_size or image_end < len(image_scan):
            return f"merged photo end {image_end} is inconsistent with the video offset {video_offset}"
        with open(output_path, "rb") as out:
            head = out.read(image_end - len(image_scan) + 4)
            out.seek(image_end - len(image_scan))
            scan = out.read(len(image_scan))
        if _jpeg_sos_offset(head) != image_end - len(image_scan):
            return "merged photo header is not a well formed JPEG that ends where the picture data starts"
        if scan != image_scan:
            return "picture data of the merged photo differs from the original"
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


def looks_like_video(head: bytes) -> bool:
    """True if the first bytes look like an MP4/MOV: an `ftyp` box, or a `wide`
    (free space) box first. The bytes are read once by the caller, then every
    signature is checked against the same bytes."""
    return any(sig in head[:16] for sig in VIDEO_SIGNATURES)


def find_embedded_video(path, use_xmp: bool = True, check_signature: bool = True):
    """(offset, length) of the video embedded in a Motion Photo, or None.
    Checks the Samsung trailer first (tail bytes only), then, if use_xmp, the XMP.
    check_signature=False accepts any trailer/XMP pointer without looking at the
    video bytes (used by the "never mux twice" guard, where a false alarm is safer)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            loc = _samsung_video_location(f, size)
            if loc is None and use_xmp:
                loc = _xmp_video_location(f, size)
            if loc is None or loc[0] < 0 or loc[1] <= 8 or loc[0] + loc[1] > size:
                return None
            if check_signature:
                f.seek(loc[0])
                if not looks_like_video(f.read(16)):   # read once, check every signature
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


def dispose_file(path, mode=None, root=None, folder=None, exts=None) -> str:
    """The only place originals get removed. Returns the action taken
    ("trash", "move" or "delete"). Trash failure falls back to move, never delete.
    Raises ValueError for disallowed extensions (if `exts` given) or paths outside
    root, FileNotFoundError if missing. If root is None the file's folder is the root."""
    p = Path(path).resolve()
    if exts is not None and p.suffix.lower() not in exts:
        raise ValueError(f"not a supported file: {path}")
    if root is not None and not is_inside(p, root):
        raise ValueError(f"path is outside the configured root: {path}")
    if not p.is_file():
        raise FileNotFoundError(f"file doesn't exist: {path}")
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


def remove_video(path, mode=None, root=None, folder=None) -> str:
    """Video-only wrapper around dispose_file (keeps the old name and checks)."""
    if Path(path).suffix.lower() not in VIDEO_EXTS:
        raise ValueError(f"not a video file: {path}")
    return dispose_file(path, mode, root, folder, VIDEO_EXTS)


def describe_destination(mode, root=None, folder=None) -> str:
    """Human text for dry-run reports."""
    mode = normalize_mode(mode)
    if mode == "trash":
        return "recycle bin (falls back to a folder if unavailable)"
    if mode == "move":
        return f"folder {removal_folder(root, folder) if root else (folder or DEFAULT_FOLDER_NAME)}"
    return "permanently deleted"


# ─── Recycle Bin guard ───────────────────────────────────────────────────────
# The Windows Recycle Bin has a per-drive size cap; an item that does not fit is
# deleted for good, and older bin items can be purged to make room. So a run
# that would send a lot to the bin refuses "trash" up front (never a silent delete).

BIN_REFUSAL = "This run is too big for the Recycle Bin. Use 'Move to folder' instead."
BIN_MAX_FILES = 500            # more items than this never go to the bin (override: L2M_BIN_MAX_FILES)
BIN_MAX_FRACTION = 0.5         # estimate + current usage may use at most this share of the cap
BIN_DEFAULT_FRACTION = 0.05    # cap assumed when the real one can't be read: 5% of the drive


class BinTooBig(Exception):
    """Raised when mode "trash" is refused for a run (message is user-facing)."""


def _drive_root(path) -> str:
    p = Path(path).resolve()
    return p.drive + "\\" if p.drive else str(p.anchor or "/")


def _volume_guid(drive_root: str) -> Optional[str]:
    import ctypes
    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive_root, buf, 64):
        return None
    s = buf.value                       # \\?\Volume{GUID}\
    a, b = s.find("{"), s.find("}")
    return s[a:b + 1].upper() if a >= 0 < b else None


def read_bin_settings(path) -> dict:
    """{"cap": bytes or None, "nuke": bool, "source": str} for the drive of `path`.
    Windows: HKCU\\...\\Explorer\\BitBucket\\Volume\\{GUID} (MaxCapacity in MB, NukeOnDelete).
    Anything else, or an unreadable key: cap None (the caller applies the default)."""
    if os.name != "nt":
        return {"cap": None, "nuke": False, "source": "default"}
    try:
        import winreg
        guid = _volume_guid(_drive_root(path))
        if not guid:
            raise OSError("no volume GUID")
        base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
        out = {"cap": None, "nuke": False, "source": "default"}
        for sub in (base + "\\Volume\\" + guid, base):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                    for name in ("MaxCapacity", "NukeOnDelete"):
                        try:
                            val = winreg.QueryValueEx(k, name)[0]
                        except OSError:
                            continue
                        if name == "MaxCapacity" and out["cap"] is None and val:
                            out["cap"], out["source"] = int(val) * 1024 * 1024, "registry"
                        elif name == "NukeOnDelete" and val:
                            out["nuke"] = True
            except OSError:
                continue
        return out
    except Exception:
        return {"cap": None, "nuke": False, "source": "default"}


def read_bin_usage(path) -> Optional[int]:
    """Bytes now in the bin of this drive (this user's items), or None if unknown."""
    if os.name != "nt":
        return None
    total, seen = 0, False
    try:
        for sid in os.scandir(_drive_root(path) + "$Recycle.Bin"):
            try:
                for dirpath, _dirs, files in os.walk(sid.path):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                    seen = True
            except OSError:
                continue
    except OSError:
        return None
    return total if seen else None


def drive_size(path) -> int:
    return shutil.disk_usage(_drive_root(path)).total


def bin_report(total_bytes: int, file_count: int, path) -> dict:
    """Judge a run that would send `total_bytes` in `file_count` items to the bin.
    Returns a dict with ok, reason (user-facing, empty if ok), estimate, cap, usage,
    nuke, cap_source. L2M_BIN_CAP_MB / L2M_BIN_MAX_FILES override cap and file limit."""
    s = read_bin_settings(path)
    cap, source = s["cap"], s["source"]
    env_cap = os.environ.get("L2M_BIN_CAP_MB")
    if env_cap and env_cap.isdigit():
        cap, source = int(env_cap) * 1024 * 1024, "override"
    if cap is None:
        try:
            cap = int(drive_size(path) * BIN_DEFAULT_FRACTION)
        except OSError:
            cap = 0
        source = "default"
    usage = read_bin_usage(path)
    max_files = BIN_MAX_FILES
    if (env_files := os.environ.get("L2M_BIN_MAX_FILES")) and env_files.isdigit():
        max_files = int(env_files)
    reasons = []
    if s["nuke"]:
        reasons.append("the Recycle Bin is set to delete files immediately")
    if file_count > max_files:
        reasons.append(f"{file_count} items is more than {max_files}")
    if cap and total_bytes + (usage or 0) > cap * BIN_MAX_FRACTION:
        reasons.append("it would fill more than half of the Recycle Bin")
    elif not cap:
        reasons.append("the Recycle Bin size is unknown")
    return {"ok": not reasons, "estimate": total_bytes, "files": file_count, "cap": cap, "usage": usage,
            "nuke": s["nuke"], "cap_source": source,
            "reason": (BIN_REFUSAL + " (" + "; ".join(reasons) + ")") if reasons else ""}


def check_bin(total_bytes: int, file_count: int, path) -> dict:
    """bin_report, raising BinTooBig when the run must not use the bin."""
    rep = bin_report(total_bytes, file_count, path)
    if not rep["ok"]:
        raise BinTooBig(rep["reason"])
    return rep


def file_size(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
