"""Safe removal of source videos after muxing (shared by Muxer.py and app.py)."""

import logging
import os
import shutil

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
