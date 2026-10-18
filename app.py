#!/usr/bin/env python3
"""Live2Motion Photos"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR / "MotionPhoto2"))
import video_removal  # noqa: E402  (shared with the muxer so all video removal goes through one helper)
CONFIG_FILE = BASE_DIR / "config.json"

# MOTIONPHOTO2_BIN can override with a path to a compiled binary; otherwise
# always run the vendored source directly (MotionPhoto2/) so bugfixes there
# take effect without ever needing to build a binary, locally or on the server.
BIN = [os.environ["MOTIONPHOTO2_BIN"]] if os.environ.get("MOTIONPHOTO2_BIN") else [
    sys.executable, str(BASE_DIR / "MotionPhoto2" / "motionphoto2.py")
]

DEFAULT_CONFIG = {
    "input_directory": "/home/ramin/shared",
    "output_directory": "",
    "recursive": True,
    "exif_match": True,
    "incremental_mode": False,
    "copy_unmuxed": False,
    "overwrite": True,
    "delete_video": False,
    "video_removal_mode": video_removal.default_mode(),
    "video_removal_folder": "",   # "move" mode only; empty = <input_directory>/_deleted_videos
    "keep_temp": False,
    "no_xmp": False,
    "verbose": False,
    "schedule_enabled": False,
    "schedule_cron": "0 2 * * *",
    "watch_enabled": False,
    "watch_debounce": 300,
}

WATCH_EXTS = {".heic", ".jpeg", ".jpg", ".mov", ".mp4"}


# ─── Shared run state ─────────────────────────────────────────────────────────

class RunState:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.process: Optional[subprocess.Popen] = None
        self.trigger: Optional[str] = None
        self.start_time: Optional[str] = None
        self.lines: list = []
        self.stats: dict = {"total": 0, "current": 0, "converted": 0, "skipped": 0, "errors": 0, "current_file": ""}
        self.history: list = []
        self.skipped_files: list = []      # full paths of already-muxed photos
        self.input_dir: str = ""

    def start(self, trigger: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.trigger = trigger
            self.start_time = datetime.now().isoformat()
            self.lines = []
            self.stats = {"total": 0, "current": 0, "converted": 0, "skipped": 0, "errors": 0, "current_file": ""}
            self.skipped_files = []
            return True

    def emit(self, raw: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {raw}"
        self.lines.append(line)
        rawl = raw.lower()
        if re.match(r"=+\[", raw):
            m = re.search(r"\[(\d+)/(\d+)\]", raw)
            if m:
                self.stats["current"] = int(m.group(1))
                self.stats["total"] = int(m.group(2))
        elif "Writing output file" in raw:
            self.stats["converted"] += 1
        elif "[ERROR]" in raw:
            self.stats["errors"] += 1
        elif "is already a motion photo, skipping" in rawl:
            self.stats["skipped"] += 1
            # "Input <rel_path> is already a motion photo, skipping muxing..."
            m = re.search(r"^Input (.+?) is already a motion photo", raw, re.IGNORECASE)
            if m and self.input_dir:
                full = str(Path(self.input_dir) / m.group(1).strip())
                if full not in self.skipped_files:
                    self.skipped_files.append(full)
        elif "no matching video" in rawl or "no matching" in rawl:
            self.stats["skipped"] += 1
        # Track currently processing filename for the status display
        if "- Processing " in raw:
            m = re.search(r"- Processing (.+)$", raw)
            if m:
                self.stats["current_file"] = Path(m.group(1).strip()).name

    def finish(self, exit_code: int):
        elapsed = ""
        if self.start_time:
            s = (datetime.now() - datetime.fromisoformat(self.start_time)).seconds
            elapsed = f"{s // 60}m {s % 60}s"
        with self._lock:
            self.history.insert(0, {
                "time": (self.start_time or "")[:19].replace("T", " "),
                "trigger": self.trigger,
                "duration": elapsed,
                "converted": self.stats["converted"],
                "skipped": self.stats["skipped"],
                "errors": self.stats["errors"],
                "exit_code": exit_code,
            })
            self.history = self.history[:20]
            self.running = False
            self.process = None

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "trigger": self.trigger,
            "start_time": self.start_time,
            "stats": dict(self.stats),
            "line_count": len(self.lines),
            "history": list(self.history),
        }


run_state = RunState()
scheduler = BackgroundScheduler()
watch_observer: Optional[Observer] = None


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
            # Older configs have no mode (or a bad one); fall back to the default
            cfg["video_removal_mode"] = video_removal.normalize_mode(cfg.get("video_removal_mode"))
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ─── Run logic ────────────────────────────────────────────────────────────────

def build_cmd(cfg: dict) -> list:
    cmd = list(BIN)
    if d := cfg.get("input_directory"):
        cmd += ["--input-directory", d]
    if d := cfg.get("output_directory"):
        cmd += ["--output-directory", d]
    if cfg.get("delete_video"):
        cmd += ["--video-removal-mode", video_removal.normalize_mode(cfg.get("video_removal_mode"))]
        if cfg.get("video_removal_folder"):
            cmd += ["--video-removal-folder", cfg["video_removal_folder"]]
    for key, flag in [
        ("recursive",       "--recursive"),
        ("exif_match",      "--exif-match"),
        ("incremental_mode","--incremental-mode"),
        ("copy_unmuxed",    "--copy-unmuxed"),
        ("overwrite",       "--overwrite"),
        ("delete_video",    "--delete-video"),
        ("keep_temp",       "--keep-temp"),
        ("no_xmp",          "--no-xmp"),
        ("verbose",         "--verbose"),
    ]:
        if cfg.get(key):
            cmd.append(flag)
    return cmd


def do_run(trigger: str = "manual"):
    if not run_state.start(trigger):
        return
    cfg = load_config()
    run_state.input_dir = cfg.get("input_directory", "")
    cmd = build_cmd(cfg)
    run_state.emit(f"▶ Triggered by: {trigger}")
    run_state.emit("$ " + " ".join(cmd))
    if not Path(BIN[-1]).exists():
        run_state.emit(f"✗ MotionPhoto2 entry point not found: {BIN[-1]}")
        run_state.finish(127)
        return
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        run_state.process = proc
        for raw in proc.stdout:
            run_state.emit(raw.rstrip())
        proc.wait()
        run_state.emit(f"{'✓' if proc.returncode == 0 else '✗'} Exited with code {proc.returncode}")
        run_state.finish(proc.returncode)
    except Exception as exc:
        run_state.emit(f"✗ Exception: {exc}")
        run_state.finish(-1)


# ─── Scheduler ────────────────────────────────────────────────────────────────

def apply_schedule(cfg: dict):
    try:
        scheduler.remove_job("mp_cron")
    except Exception:
        pass
    if cfg.get("schedule_enabled") and cfg.get("schedule_cron"):
        try:
            scheduler.add_job(
                lambda: threading.Thread(target=do_run, args=("schedule",), daemon=True).start(),
                CronTrigger.from_crontab(cfg["schedule_cron"]),
                id="mp_cron", replace_existing=True,
            )
        except Exception as e:
            print(f"[scheduler] {e}")


# ─── File watcher ─────────────────────────────────────────────────────────────

class MediaHandler(FileSystemEventHandler):
    def __init__(self, debounce: int, skip_folder: Optional[Path] = None):
        self._skip_folder = skip_folder
        self._debounce = max(debounce, 10)
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _relevant(self, path: str) -> bool:
        if self._skip_folder and video_removal.is_inside(path, self._skip_folder):
            return False   # videos moved aside by "move" mode must not retrigger a run
        return Path(path).suffix.lower() in WATCH_EXTS

    def on_created(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self._reset()

    def on_moved(self, event):
        if not event.is_directory and self._relevant(event.dest_path):
            self._reset()

    def _reset(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(
                self._debounce,
                lambda: threading.Thread(target=do_run, args=("watch",), daemon=True).start(),
            )
            self._timer.start()


def start_watcher(cfg: dict):
    global watch_observer
    stop_watcher()
    path = cfg.get("input_directory", "")
    if path and os.path.isdir(path):
        watch_observer = Observer()
        skip = video_removal.removal_folder(path, cfg.get("video_removal_folder"))
        watch_observer.schedule(MediaHandler(cfg.get("watch_debounce", 300), skip), path, recursive=True)
        watch_observer.start()


def stop_watcher():
    global watch_observer
    if watch_observer:
        watch_observer.stop()
        try:
            watch_observer.join(timeout=5)
        except Exception:
            pass
        watch_observer = None


# ─── FastAPI ──────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler.start()
    cfg = load_config()
    apply_schedule(cfg)
    if cfg.get("watch_enabled"):
        start_watcher(cfg)
    yield
    scheduler.shutdown(wait=False)
    stop_watcher()

app = FastAPI(title="Live2Motion Photos", lifespan=lifespan)


@app.get("/")
def root():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
def api_get_config():
    return load_config()


@app.post("/api/config")
async def api_save_config(request: Request):
    cfg = await request.json()
    save_config(cfg)
    apply_schedule(cfg)
    if cfg.get("watch_enabled"):
        start_watcher(cfg)
    else:
        stop_watcher()
    return {"ok": True}


@app.post("/api/run")
def api_run():
    if run_state.running:
        return JSONResponse({"error": "Already running"}, status_code=409)
    threading.Thread(target=do_run, args=("manual",), daemon=True).start()
    return {"ok": True}


@app.post("/api/cancel")
def api_cancel():
    proc = run_state.process
    if proc:
        proc.terminate()
        return {"ok": True}
    return JSONResponse({"error": "Not running"}, status_code=400)


@app.get("/api/status")
def api_status():
    snap = run_state.snapshot()
    job = scheduler.get_job("mp_cron")
    snap["next_run"] = job.next_run_time.isoformat() if job and job.next_run_time else None
    snap["watch_active"] = watch_observer is not None and watch_observer.is_alive()
    return snap


@app.get("/api/progress")
async def api_progress(request: Request, from_line: int = 0):
    """SSE stream — delivers all lines for the current (or next) run."""
    async def generate():
        yield "retry: 2000\n\n"
        cursor = from_line
        was_running = False

        while True:
            if await request.is_disconnected():
                break

            # Drain any new lines
            lines = run_state.lines
            while cursor < len(lines):
                payload = json.dumps({
                    "line": lines[cursor],
                    "index": cursor,
                    "stats": dict(run_state.stats),
                })
                yield f"data: {payload}\n\n"
                cursor += 1

            if run_state.running:
                was_running = True
            elif was_running and cursor >= len(run_state.lines):
                # Run ended and all lines delivered
                payload = json.dumps({
                    "done": True,
                    "stats": dict(run_state.stats),
                    "history": list(run_state.history),
                })
                yield f"data: {payload}\n\n"
                break

            await asyncio.sleep(0.15)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



THIS_PC = "This PC"  # sentinel path: virtual root listing all Windows drives


def list_windows_drives() -> list:
    if sys.platform != "win32":
        return []
    import ctypes
    import string
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if bitmask & (1 << i)]


@app.get("/api/browse")
def api_browse(path: str = ""):
    if path == THIS_PC:
        return {"path": THIS_PC, "parent": None, "dirs": list_windows_drives(),
                "sep": os.sep, "root_list": True}
    try:
        p = (Path(path) if path else Path.home()).resolve()
    except Exception:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not p.exists() or not p.is_dir():
        # Saved config paths are sometimes POSIX-style (e.g. "/data" from the
        # Docker default) and don't exist as-is on Windows, where a leading
        # "/" resolves to the root of the current drive. Fall back to the
        # user's home directory instead of leaving the browser dead-ended.
        home = Path.home().resolve()
        if p != home:
            return api_browse(str(home))
        return JSONResponse({"error": "Not a directory"}, status_code=404)
    def is_accessible_dir(entry: Path) -> bool:
        try:
            return entry.is_dir()
        except OSError:
            return False   # e.g. macOS ~/.Trash has an ACL that blocks stat entirely

    # On Windows, a drive's parent is itself (there's no shared root across
    # drives) - send the user to the virtual drive list instead of a dead end.
    at_windows_drive_root = p == p.parent and sys.platform == "win32"
    parent = THIS_PC if at_windows_drive_root else (str(p.parent) if p != p.parent else None)

    try:
        dirs = sorted(
            [d.name for d in p.iterdir() if not d.name.startswith(".") and is_accessible_dir(d)],
            key=str.lower,
        )
        # sep tells the frontend how to join/split this path (Windows uses "\", not "/")
        return {"path": str(p), "parent": parent, "dirs": dirs, "sep": os.sep}
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)


VIDEO_EXTS = {".mov", ".mp4"}
PHOTO_EXTS = {".heic", ".jpg", ".jpeg"}
MAX_LIVE_PHOTO_SECS = 5.0


def _batch_tag(paths: list[str], tag: str) -> dict[str, str]:
    """Return {path: tag_value} for a list of files, via one exiftool call."""
    values: dict[str, str] = {}
    if not paths:
        return values
    chunk = 500
    for i in range(0, len(paths), chunk):
        batch = paths[i:i + chunk]
        try:
            r = subprocess.run(
                ["exiftool", "-q", "-p", f"$SourceFile\t${tag}"] + batch,
                capture_output=True, text=True, timeout=60,
            )
            for line in r.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[1].strip():
                    values[parts[0]] = parts[1].strip()
        except Exception:
            pass
    return values


def _batch_durations(video_paths: list[str]) -> dict[str, float]:
    """Return {path: duration_secs} for a list of video files, via one exiftool call."""
    durations: dict[str, float] = {}
    if not video_paths:
        return durations
    chunk = 500
    for i in range(0, len(video_paths), chunk):
        batch = video_paths[i:i + chunk]
        try:
            r = subprocess.run(
                ["exiftool", "-q", "-p", "$SourceFile\t$Duration#"] + batch,
                capture_output=True, text=True, timeout=60,
            )
            for line in r.stdout.splitlines():
                parts = line.strip().split("\t", 1)
                if len(parts) == 2:
                    try:
                        durations[parts[0]] = float(parts[1])
                    except ValueError:
                        durations[parts[0]] = 999.0
        except Exception:
            pass
    return durations


def _walk_media(input_dir: str) -> tuple[list[str], list[str]]:
    """Recursively collect (photo_paths, video_paths) under input_dir, any subfolder."""
    photo_paths, video_paths = [], []
    skip = video_removal.removal_folder(input_dir, load_config().get("video_removal_folder"))
    for root, _dirs, files in os.walk(input_dir, followlinks=True):
        _dirs[:] = [d for d in _dirs if Path(root, d).resolve() != skip]   # don't rescan removed videos
        try:
            for f in files:
                fp = Path(root) / f
                suf = fp.suffix.lower()
                if suf in VIDEO_EXTS:
                    video_paths.append(str(fp))
                elif suf in PHOTO_EXTS:
                    photo_paths.append(str(fp))
        except OSError:
            continue   # skip unreadable/unmounted symlink targets
    return photo_paths, video_paths


def _pair_by_content_id(photo_paths: list[str], video_paths: list[str]) -> list[tuple[str, str]]:
    """Pair photos to videos by Apple's ContentIdentifier tag — the same identifier
    MotionPhoto2's --exif-match mode uses (motionphoto2.py's content_id_to_video map),
    so folder location and filename never matter, only the id embedded in both files."""
    photo_ids = _batch_tag(photo_paths, "MakerNotes:ContentIdentifier")
    video_ids = _batch_tag(video_paths, "QuickTime:ContentIdentifier")

    id_to_video: dict[str, str] = {}
    for path, cid in video_ids.items():
        id_to_video.setdefault(cid, path)

    return [(photo, id_to_video[cid]) for photo, cid in photo_ids.items() if cid in id_to_video]


def _filter_deletable(pairs: list[tuple[str, str]]) -> tuple[list[dict], int]:
    """Given (photo, video) pairs matched by ContentIdentifier, keep only those where
    the video is short enough to be a Live Photo clip AND the photo is confirmed (via
    exiftool) to already have the video embedded — only then is the standalone video
    truly redundant and safe to offer for deletion."""
    if not pairs:
        return [], 0

    # Standalone clips longer than a Live Photo must never be offered for deletion,
    # even if they coincidentally share a ContentIdentifier lookup.
    vid_paths = list({v for _, v in pairs})
    durations = _batch_durations(vid_paths)
    pairs = [(p, v) for p, v in pairs if durations.get(v, 999.0) <= MAX_LIVE_PHOTO_SECS]
    if not pairs:
        return [], 0

    # Always verify via exiftool that the photo actually has the video embedded
    # before offering the paired video for deletion — a ContentIdentifier match alone
    # doesn't guarantee the mux has actually happened yet.
    photo_paths = list({p for p, _ in pairs})
    muxed: set[str] = set()
    chunk = 500
    for i in range(0, len(photo_paths), chunk):
        try:
            r = subprocess.run(
                ["exiftool", "-q", "-m", "-if", "$MotionPhoto",
                 "-p", "$SourceFile"] + photo_paths[i:i + chunk],
                capture_output=True, text=True, timeout=60,
            )
            muxed.update(l.strip() for l in r.stdout.splitlines() if l.strip())
        except Exception:
            pass

    videos, total_bytes, seen = [], 0, set()
    for photo, vid in pairs:
        if photo in muxed and vid not in seen:
            seen.add(vid)
            try:
                size = Path(vid).stat().st_size
            except OSError:
                continue
            videos.append({"path": vid, "name": Path(vid).name, "size": size})
            total_bytes += size
    return videos, total_bytes


@app.get("/api/cleanup-preview")
def api_cleanup_preview():
    """Videos paired (by ContentIdentifier) with photos logged as skipped (already
    muxed) in the last run. Searches the whole input directory tree, not just the
    photo's own folder, since the true pair can live in any subfolder."""
    photos = run_state.skipped_files
    if not photos or not run_state.input_dir or not Path(run_state.input_dir).is_dir():
        return {"count": 0, "total_bytes": 0, "videos": [], "skipped_photos": len(photos)}

    _, video_paths = _walk_media(run_state.input_dir)
    pairs = _pair_by_content_id(photos, video_paths)
    videos, total_bytes = _filter_deletable(pairs)
    return {"count": len(videos), "total_bytes": total_bytes, "videos": videos,
            "skipped_photos": len(photos)}


@app.get("/api/cleanup-scan")
def api_cleanup_scan():
    """Walk the whole input directory tree and find video files whose ContentIdentifier
    matches a photo that's already a motion photo — mirroring exactly how MotionPhoto2's
    --exif-match mode pairs files, so folder location and filename never matter."""
    cfg = load_config()
    input_dir = cfg.get("input_directory", "")
    if not input_dir or not Path(input_dir).is_dir():
        return JSONResponse({"error": "Input directory not configured or not found"}, status_code=400)

    photo_paths, video_paths = _walk_media(input_dir)
    if not photo_paths or not video_paths:
        return {"count": 0, "total_bytes": 0, "videos": [], "scanned": 0}

    pairs = _pair_by_content_id(photo_paths, video_paths)
    videos, total_bytes = _filter_deletable(pairs)
    return {"count": len(videos), "total_bytes": total_bytes, "videos": videos,
            "scanned": len(pairs)}


@app.delete("/api/cleanup")
async def api_cleanup(request: Request):
    """Remove a list of video files by path (inside the input directory only),
    using the configured video_removal_mode."""
    body = await request.json()
    paths = body.get("paths", [])
    cfg = load_config()
    root = cfg.get("input_directory", "")
    if not root or not Path(root).is_dir():
        return JSONResponse({"error": "Input directory not configured or not found"}, status_code=400)
    deleted, errors = [], []
    for path in paths:
        try:
            video_removal.remove_video(path, cfg["video_removal_mode"], root, cfg.get("video_removal_folder"))
            deleted.append(path)
        except Exception as e:
            errors.append({"file": path, "error": str(e)})
    return {"deleted": len(deleted), "errors": errors}


DEFAULT_PORT = 7000
PORT_FILE = BASE_DIR / ".port"


def find_available_port(preferred: int, host: str = "0.0.0.0", attempts: int = 50) -> int:
    """Return `preferred` if free, otherwise the next free port after it."""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available port found in range {preferred}-{preferred + attempts - 1}")


if __name__ == "__main__":
    port = find_available_port(DEFAULT_PORT)
    if port != DEFAULT_PORT:
        print(f"Port {DEFAULT_PORT} is already in use - using port {port} instead.")
    try:
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass
    print(f"Live2Motion Photos will be available at http://localhost:{port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
