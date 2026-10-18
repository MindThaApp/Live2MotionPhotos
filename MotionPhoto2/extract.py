"""Extract mode: split a Motion Photo into a still image plus a separate video.

Supported layouts: Google Motion Photo v1 (XMP Container:Directory), legacy
MicroVideo (GCamera:MicroVideoOffset = bytes from the end of the file) and the
Samsung trailer (MotionPhoto_Data / SEFT). The video is cut out with plain byte
slicing; exiftool is only used (when installed) to copy the capture date and GPS
onto the video and to strip the motion-photo XMP flags from the still image.
"""

import os
import re
import shutil
import struct
import tempfile

from pathlib import Path
from typing import Callable, Optional

from video_removal import find_embedded_video

EXTRACT_DIRNAME = "_extracted"   # default output folder; the converter and watcher never scan it
IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".avif"}
SAMSUNG_MARKER = b"MotionPhoto_Data"
# ftyp brands that belong to an image (a HEIC/AVIF motion photo starts with one)
IMAGE_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
# brands whose file can be called .mov without touching a single byte
MOV_SAFE_BRANDS = {b"qt  ", b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"M4VH", b"M4VP"}
MAX_XMP_SCAN = 4 * 1024 * 1024


class ExtractError(Exception):
    """A file that cannot be extracted (reported per file, never fatal)."""


# ─── Locating the video ───────────────────────────────────────────────────────

def has_ftyp(data: bytes, start: int) -> bool:
    return start >= 0 and data[start + 4:start + 8] == b"ftyp"


def _ftyp_brand(data: bytes, start: int) -> bytes:
    return data[start + 8:start + 12]


def _walk_boxes(data: bytes, start: int):
    """Top-level MP4 boxes from start as [(type, offset, size)]; stops at the first
    box that doesn't fit. Used to find where a scanned video really ends."""
    boxes, pos, n = [], start, len(data)
    while pos + 8 <= n:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if size == 1:
            if pos + 16 > n:
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        elif size == 0:
            size = n - pos
        if size < 8 or pos + size > n or not re.fullmatch(rb"[\x20-\x7e\xa9]{4}", kind):
            break
        boxes.append((kind, pos, size))
        pos += size
    return boxes


def _xmp_text(data: bytes) -> str:
    start = data.find(b"<x:xmpmeta", 0, MAX_XMP_SCAN)
    if start == -1:
        return ""
    end = data.find(b"</x:xmpmeta>", start)
    end = start + MAX_XMP_SCAN if end == -1 else end + 12
    return data[start:end].decode("utf-8", "replace")


def _xmp_info(data: bytes) -> dict:
    """What the XMP claims: motion photo flagged?, MicroVideoOffset, video item length."""
    xmp = _xmp_text(data)
    info = {"flagged": False, "micro_offset": None, "item_length": None}
    if not xmp:
        return info
    if re.search(r"GCamera:(MotionPhoto|MicroVideo)\s*=\s*[\"']1[\"']", xmp) \
            or re.search(r"<GCamera:(MotionPhoto|MicroVideo)>\s*1\s*<", xmp) \
            or re.search(r"Item:Semantic\s*=\s*[\"']MotionPhoto[\"']", xmp):
        info["flagged"] = True
    m = re.search(r"GCamera:MicroVideoOffset\s*=\s*[\"'](\d+)[\"']", xmp) \
        or re.search(r"<GCamera:MicroVideoOffset>\s*(\d+)\s*<", xmp)
    if m:
        info["micro_offset"] = int(m.group(1))
    return info


def _scan_for_video(data: bytes, begin: int = 0) -> Optional[int]:
    """Offset of the first plausible video `ftyp` box at/after begin (skipping the
    image's own HEIC/AVIF ftyp), or None."""
    pos = max(begin, 0)
    while True:
        i = data.find(b"ftyp", pos + 4)
        if i == -1:
            return None
        start = i - 4
        size = struct.unpack(">I", data[start:start + 4])[0] if start >= 0 else 0
        brand = _ftyp_brand(data, start)
        if start >= begin and 16 <= size <= 512 and brand not in IMAGE_BRANDS and start + size <= len(data) \
                and re.fullmatch(rb"[\x20-\x7e]{4}", brand):
            return start
        pos = i


class Located:
    """Where the video is: data[start:end], plus how it was found."""

    def __init__(self, start: int, end: int, method: str, note: str = ""):
        self.start, self.end, self.method, self.note = start, end, method, note


def locate_video(data: bytes, path=None, et=None) -> Optional[Located]:
    """Find the embedded video in a motion photo, or None if there isn't one.
    Offsets from the metadata are validated (the video must start with an ftyp
    box); if they are wrong the bytes are scanned for ftyp instead."""
    size = len(data)
    # 1. Samsung trailer, then Google v1 XMP (shared with the muxer / removal code)
    if path is not None:
        loc = find_embedded_video(path, use_xmp=False)
        method = "samsung trailer"
        if loc is None:
            loc = find_embedded_video(path, use_xmp=True)
            method = "motion photo v1 (XMP)"
        if loc is not None and has_ftyp(data, loc[0]):
            return Located(loc[0], loc[0] + loc[1], method)
    # 2. Legacy MicroVideo: offset counts back from the end of the file
    info = _xmp_info(data)
    micro = info["micro_offset"]
    if micro and 8 < micro <= size and has_ftyp(data, size - micro):
        return Located(size - micro, size, "MicroVideo")
    # 3. Offsets absent or wrong: is it flagged at all? Then scan for ftyp.
    marker = data.rfind(SAMSUNG_MARKER)
    samsung = marker != -1 and data[-4:] == b"SEFT"
    if not (info["flagged"] or micro or samsung):
        return _exiftool_fallback(data, path, et)
    begin = marker + len(SAMSUNG_MARKER) if samsung else 0
    start = _scan_for_video(data, begin)
    if start is None:
        return None
    boxes = _walk_boxes(data, start)
    end = boxes[-1][1] + boxes[-1][2] if boxes else size
    return Located(start, end, "scan for ftyp", "metadata offset was missing or wrong")


def _exiftool_fallback(data: bytes, path, et) -> Optional[Located]:
    """Last resort for unusual layouts: ask exiftool (the existing detection)."""
    if et is None or path is None:
        return None
    from utils import is_motion_photo, extract_video_from_image
    try:
        if not is_motion_photo(path, et):
            return None
        video = extract_video_from_image(path, et)
    except Exception:
        return None
    if not video or not has_ftyp(video, 0):
        return None
    start = data.rfind(video[:64])
    if start == -1:
        return None
    return Located(start, start + len(video), "exiftool")


# ─── Output helpers ───────────────────────────────────────────────────────────

def mov_safe(video: bytes) -> bool:
    """True if the video can be saved as .mov unchanged: plain (non-fragmented)
    ISO/QuickTime file with a known brand."""
    if not has_ftyp(video, 0):
        return False
    brands = {_ftyp_brand(video, 0)}
    size = struct.unpack(">I", video[:4])[0]
    brands |= {video[i:i + 4] for i in range(16, min(size, len(video)), 4)}
    if not brands & MOV_SAFE_BRANDS or brands & {b"3gp4", b"3gp5", b"3gp6", b"3g2a"}:
        return False
    kinds = [k for k, _, _ in _walk_boxes(video, 0)]
    return b"moov" in kinds and b"moof" not in kinds


def jpeg_still_end(data: bytes, video_start: int) -> Optional[int]:
    """End of the JPEG image data before the trailer (Samsung tag bytes included
    in the trailer), or None if this is not a JPEG."""
    if data[:2] != b"\xff\xd8":
        return None
    eoi = data.rfind(b"\xff\xd9", 0, video_start)
    return eoi + 2 if eoi != -1 and eoi >= video_start - 256 else video_start


def publish_new(tmp: str, dest: Path) -> Path:
    """Give the finished temp file its final name without ever replacing an
    existing file: a taken name gets a numeric suffix (name_1.mp4, ...)."""
    n = 0
    while True:
        cand = dest if n == 0 else dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
        try:
            os.link(tmp, cand)            # atomic and fails if cand exists
        except FileExistsError:
            continue
        except OSError:                   # no hard links on this file system
            if cand.exists():
                continue
            os.replace(tmp, cand)
            return cand
        os.unlink(tmp)
        return cand


def _write_temp(directory: Path, data: bytes, suffix: str) -> str:
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".mp2tmp.", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def copy_capture_metadata(et, photo: str, video_tmp: str) -> str:
    """Copy capture date and GPS from the photo onto the video (QuickTime
    CreateDate/ModifyDate, Keys CreationDate + GPSCoordinates). Returns a note."""
    tags = et.get_tags([photo], ["DateTimeOriginal", "CreateDate", "OffsetTimeOriginal",
                                 "GPSLatitude", "GPSLongitude", "GPSAltitude"], params=["-n"])
    got = {k.split(":")[-1]: v for k, v in (tags[0] if tags else {}).items()}
    args, done = [], []
    date = got.get("DateTimeOriginal") or got.get("CreateDate")
    if date:
        stamp = f"{date}{got.get('OffsetTimeOriginal') or ''}"
        args += [f"-QuickTime:CreateDate={stamp}", f"-QuickTime:ModifyDate={stamp}", f"-Keys:CreationDate={stamp}"]
        done.append("date")
    lat, lon = got.get("GPSLatitude"), got.get("GPSLongitude")
    if lat is not None and lon is not None:
        coords = f"{lat} {lon}" + (f" {got['GPSAltitude']}" if got.get("GPSAltitude") is not None else "")
        args += [f"-Keys:GPSCoordinates={coords}", f"-UserData:GPSCoordinates={coords}"]
        done.append("GPS")
    if not args:
        return "photo has no date/GPS to copy"
    et.execute("-overwrite_original", "-api", "QuickTimeUTC=1", *args, video_tmp)
    return "copied " + " + ".join(done)


def strip_motion_xmp(et, still_tmp: str) -> str:
    et.execute("-overwrite_original", "-XMP-GCamera:all=", "-XMP-GContainer:all=", still_tmp)
    return "motion photo XMP removed"


# ─── Extraction ───────────────────────────────────────────────────────────────

def plan_extract(photo: str, et=None) -> tuple:
    """(data, Located) or raises ExtractError. No file is written."""
    try:
        data = Path(photo).read_bytes()
    except OSError as e:
        raise ExtractError(f"cannot read file: {e}")
    loc = locate_video(data, photo, et)
    if loc is None:
        raise ExtractError("not a motion photo")
    return data, loc


def extract_one(photo: str, out_dir: str, make_mov: bool = False, strip_still: bool = False,
                et=None, dry_run: bool = False, data: bytes = None, loc: Located = None) -> dict:
    """Extract one photo. Returns {"outputs": [paths], "notes": [str]}; raises
    ExtractError if there is nothing to extract. In dry_run nothing is written
    (the outputs are the names that would be used)."""
    if loc is None:
        data, loc = plan_extract(photo, et)
    video = data[loc.start:loc.end]
    if not has_ftyp(video, 0):
        raise ExtractError("extracted data is not a valid MP4 (no ftyp box)")
    p = Path(photo)
    out = Path(out_dir)
    notes = [loc.method + (f" ({loc.note})" if loc.note else "")]
    as_mov = bool(make_mov) and mov_safe(video)
    if make_mov and not as_mov:
        notes.append("kept .mp4 (container not safe to rename)")
    video_dest = out / (p.stem + (".mov" if as_mov else ".mp4"))
    still_dest = None
    still_end = None
    if strip_still:
        still_end = jpeg_still_end(data, loc.start)
        if still_end is None:
            notes.append("stripped still skipped (JPEG only)")
        else:
            still_dest = out / f"{p.stem}_still{p.suffix}"
    if dry_run:
        return {"outputs": [str(video_dest)] + ([str(still_dest)] if still_dest else []), "notes": notes}

    out.mkdir(parents=True, exist_ok=True)
    mtime = os.path.getmtime(photo)
    tmps = []
    try:
        vtmp = _write_temp(out, video, ".mp4")
        tmps.append(vtmp)
        stmp = None
        if still_dest:
            stmp = _write_temp(out, data[:still_end], p.suffix)
            tmps.append(stmp)
        if et is not None:
            try:
                notes.append("video: " + copy_capture_metadata(et, photo, vtmp))
            except Exception as e:
                notes.append(f"video metadata not copied ({e})")
            if stmp:
                try:
                    notes.append(strip_motion_xmp(et, stmp))
                except Exception as e:
                    notes.append(f"XMP flags not removed ({e})")
        else:
            notes.append("exiftool not found: no date/GPS copied" + (", XMP flags kept in still" if stmp else ""))
        outputs = []
        for tmp, dest in ((vtmp, video_dest), (stmp, still_dest)):
            if tmp:
                os.utime(tmp, (mtime, mtime))
                outputs.append(str(publish_new(tmp, dest)))
        return {"outputs": outputs, "notes": notes}
    finally:
        for t in tmps:
            try:
                os.unlink(t)
            except FileNotFoundError:
                pass


def _collect_images(root: Path, recursive: bool) -> list:
    out = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != EXTRACT_DIRNAME]
            out += [str((Path(dirpath) / f).relative_to(root)) for f in filenames]
    else:
        out = [f for f in os.listdir(root) if (root / f).is_file()]
    return sorted(f for f in out if Path(f).suffix.lower() in IMAGE_EXTS and not Path(f).name.startswith("."))


def run_extract(input_directory=None, input_image=None, output_directory=None, recursive=True,
                make_mov=False, strip_still=False, dry_run=False, emit: Callable = None, et=None,
                force=False) -> dict:
    """CLI/API driver. Prints progress lines the web UI understands and reports one
    result per file through emit(name, status, reason, output)."""
    emit = emit or (lambda *a, **k: None)
    if input_image:
        root = Path(input_image).resolve().parent
        files = [Path(input_image).name]
    else:
        root = Path(input_directory).resolve()
        files = _collect_images(root, recursive)
    if output_directory and Path(output_directory).is_file():
        raise ExtractError("Output directory cannot be a file")
    counts = {"extracted": 0, "skipped": 0, "errors": 0}
    print(f"{'[DRY-RUN] ' if dry_run else ''}Extracting videos from {len(files)} image(s) in {root}")
    if et is None and not dry_run:
        print("[INFO] exiftool not found: capture date/GPS will not be copied to the videos")
    for i, rel in enumerate(files, 1):
        photo = str(root / rel)
        print(f"=========================[{i}/{len(files)}]")
        print(f"- Processing {rel}")
        # Default: an _extracted subfolder, so NAME.mp4 never sits next to NAME.jpg where
        # the converter could pair it with the photo again.
        out_dir = str((Path(output_directory).resolve() / Path(rel).parent) if output_directory
                      else Path(photo).parent / EXTRACT_DIRNAME)
        done = [n for n in (Path(rel).stem + ".mp4", Path(rel).stem + ".mov") if (Path(out_dir) / n).exists()]
        if done and not force:
            print(f"{rel} already extracted ({done[0]}), skipping...")
            counts["skipped"] += 1
            emit(rel, "skipped", "already extracted")
            continue
        try:
            data, loc = plan_extract(photo, et)
            res = extract_one(photo, out_dir, make_mov, strip_still, et, dry_run, data, loc)
        except ExtractError as e:
            print(f"{rel} is not a motion photo, skipping..." if str(e) == "not a motion photo"
                  else f"[WARNING] Skipping {rel}: {e}")
            counts["skipped"] += 1
            emit(rel, "skipped", str(e))
            continue
        except Exception as e:
            print(f"[ERROR] {rel}: {e}")
            counts["errors"] += 1
            emit(rel, "error", str(e))
            continue
        verb = "[DRY-RUN] Would extract" if dry_run else "Extracted"
        print(f"{verb} {rel} -> {' + '.join(res['outputs'])} ({'; '.join(res['notes'])})")
        counts["extracted"] += 1
        emit(rel, "planned" if dry_run else "converted", "; ".join(res["notes"]), "; ".join(res["outputs"]))
    print("=" * 25)
    print(f"[SUMMARY] extracted {counts['extracted']}, skipped {counts['skipped']}, errors {counts['errors']}")
    return counts
