import exiftool
import json
import os
import re
import shutil
import tempfile

from pathlib import Path
from typing import Dict, Any

import constants as const
from video_removal import find_embedded_video


def extract_track_number(metadata: str) -> str:
    find = re.search(r"\s*<Track(\d+):StillImageTime>-1<", metadata)
    return find.group(1)


def extract_still_image_time(track_number: str, metadata: str):
    """Presentation time (microseconds, as str) of the Live Photo key frame: the
    SampleTime of the still-image-time sample in the exiftool -X output, which
    exiftool lists just before that sample's own tags. None if not available."""
    find = re.search(rf"<Track{track_number}:StillImageTime>-1<", metadata)
    if find is None:
        return None
    times = re.findall(r"<(?:Track\d+|QuickTime):SampleTime>(\d+\.?\d*)<", metadata[:find.start()])
    if not times:
        return None
    return str(round(float(times[-1]) * 1000000))


def extract_track_duration(track_number: str, metadata: str) -> str:
    find = re.search(rf"\s*<Track{track_number}:TrackDuration>(\d+\.?\d*)<", metadata)
    track_duration = find.group(1)
    if track_duration is None:
        return -1
    return str(round(float(track_duration) * 1000000))


def read_file(fpath: str) -> bytes:
    with open(fpath, "rb") as f:
        return f.read()


def atomic_write(target: str, data: bytes, stat_from: str = None) -> None:
    """Write data to target atomically: temp file in the same directory,
    flush + fsync, then os.replace. The target is untouched if anything fails."""
    directory = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mp2tmp.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if stat_from is not None:
            shutil.copystat(stat_from, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def output_with_extension(path: str, ext) -> str:
    """The output path Muxer really writes: the extension follows the image's real type
    (File:FileTypeExtension), so IMG.JPEG becomes IMG.jpg."""
    p = Path(path)
    if ext and p.suffix[1:].lower() != str(ext).lower():
        return os.path.join(p.parent, p.stem + f".{str(ext).lower()}")
    return path


def _norm_path(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def output_conflict(output: str, source: str, video: str = None, claimed=None, rerun_ok: bool = False):
    """Why writing `output` for `source` would destroy another file, or None if it is safe.

    Safe: the output is the source itself (overwrite), or does not exist yet. With
    rerun_ok (the name comes straight from the source, not overwrite mode) an existing
    file that already contains this video is an earlier result of the same conversion.
    `claimed` is a set shared by the whole run: two sources must not map to one output."""
    key = _norm_path(output)
    if claimed is not None and key in claimed:
        return f"output would overwrite {output}, which another photo in this run also writes"
    try:
        same = os.path.samefile(output, source)
    except OSError:
        same = _norm_path(output) == _norm_path(source)
    if same:
        return None
    if os.path.lexists(output):
        if rerun_ok and video and input_output_binary_compare(video, output):
            return None
        return f"output would overwrite {output}, an existing file that is not this photo"
    return None


def claim_output(claimed, output: str) -> None:
    if claimed is not None:
        claimed.add(_norm_path(output))


def enrich_fname(fpath: str, enrich: str) -> str:
    p = Path(fpath)
    fname = f"{p.stem}.{enrich}{p.suffix}"
    return os.path.join(p.parent, fname)

def exiftool_message(e) -> str:
    """exiftool's own message for a failed call (e.g. "Error: File is empty - x.jpg"),
    not pyexiftool's generic "execute returned a non-zero exit status: 1"."""
    parts = []
    for attr in ("stderr", "stdout"):
        v = getattr(e, attr, "")
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        v = str(v or "").strip()
        if attr == "stdout" and v.startswith(("[", "{")):
            try:                              # -json output: the message is in "ExifTool:Error"
                v = " ".join(str(val) for d in json.loads(v) if isinstance(d, dict)
                             for key, val in d.items() if key.rsplit(":", 1)[-1] == "Error" and val)
            except ValueError:
                pass
        if v:
            parts.append(v)
    msg = " ".join(" ".join(parts).split())
    return msg or str(e) or e.__class__.__name__


def is_motion_photo(fpath: str, et: exiftool.ExifToolHelper) -> bool:
    # 1. Samsung trailer (also what this tool writes): a few bytes from the end of
    #    the file, no exiftool and no video extraction.
    if find_embedded_video(fpath, use_xmp=False):
        return True
    # 2. Read just the Google flags. Plain photos (the common case) stop here
    #    without exiftool ever pulling out an embedded video.
    flags = et.execute("-s3", "-XMP-GCamera:MotionPhoto", "-XMP-GCamera:MicroVideo", fpath)
    if not str(flags).strip():
        return False
    # 3. Flagged: confirm that the embedded data really is a video.
    video_in_image = extract_video_from_image(fpath, et)
    return bool(video_in_image and verify_video_in_image(video_in_image))

def verify_video_in_image(video_in_image: bytes) -> bool :
    if video_in_image and any([video_in_image.find(sig, 0, 15) != -1 for sig in const.VIDEO_SIGNATURE["VIDEO"]]):
        for sig in const.VIDEO_SIGNATURE["NOT_VIDEO"]:
            if video_in_image.find(sig, 0, 15) != -1:
                return False
        return True
    return False


def extract_video_from_image(fpath: str, et: exiftool.ExifToolHelper) -> bytes:
    # Check using GCamera headers
    video_in_image = et.execute("-b",
                                "-MotionPhotoVideo",
                                fpath,
                                raw_bytes=True)

    # If not then check using samsung headers
    if not video_in_image:
        video_in_image = et.execute("-b",
                                    "-EmbeddedVideoFile",
                                    fpath,
                                    raw_bytes=True)

    return video_in_image

def input_output_binary_compare(input_video: str, output_image: str) -> bool:
    if all((os.path.exists(input_video), os.path.exists(output_image))):
        try:
            with open(input_video, "rb") as i_vid:
                input_video_data = i_vid.read()
                with open(output_image, "rb") as o_img:
                    output_image_data = o_img.read()
                    if output_image_data.find(input_video_data) != -1:
                        return True
        except:
            pass
    return False

def load_defaults() -> Dict[str, Any]:
    try:
        scriptdir = Path(__file__).resolve().parent
        with open(scriptdir / 'motionphoto2.json', 'r' , encoding='utf-8') as f:
            return json.load(f)
    except:
        return {
            'input_directory' : '',
            'recursive' : True,
            'exif_match' : True,
            'incremental_mode' : False,
            'copy_unmuxed' : False,
            'output_directory' : '',
            'delete_video' : False,
            'overwrite' : False,
            'keep_temp' : False,
            'verbose' : False,
            'input_image' : '',
            'input_video' : '',
            'output_file' : '',
            'no_xmp' : False
        }
        
def save_defaults(defaults: Dict[str, Any]):
    try:
        scriptdir = Path(__file__).resolve().parent
        with open(scriptdir / 'motionphoto2.json', 'w' , encoding='utf-8') as f: 
            json.dump(defaults, f, indent=3)       
    except:
        pass