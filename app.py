#!/usr/bin/env python3
"""Live2Motion Photos"""

import asyncio
import hmac
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Request
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
    "overwrite": False,
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
    "watch_stable_seconds": 10,   # a file must keep the same size + mtime this long before a run
}

WATCH_EXTS = {".heic", ".jpeg", ".jpg", ".mov", ".mp4"}

# The run log kept in memory (and replayed to the browser) is capped to the most
# recent lines; set LIVE2MOTION_LOG_FILE (or "log_file" in config.json) to also
# keep the complete log on disk in a size-rotated file.
MAX_LOG_LINES = int(os.environ.get("LIVE2MOTION_MAX_LOG_LINES") or 5000)
LOG_FILE_BYTES = 5 * 1024 * 1024


# ─── Shared run state ─────────────────────────────────────────────────────────

class RunState:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.process: Optional[subprocess.Popen] = None
        self.trigger: Optional[str] = None
        self.start_time: Optional[str] = None
        self.lines: deque = deque(maxlen=MAX_LOG_LINES)   # most recent lines only
        self.line_total = 0                                # lines emitted so far (absolute index of the next one)
        self.stats: dict = {"total": 0, "current": 0, "converted": 0, "skipped": 0, "errors": 0, "current_file": ""}
        self.history: list = []
        self.cancel_requested = False
        self._file_log: Optional[logging.Logger] = None
        self._file_log_path = ""
        self.skipped_files: list = []      # full paths of already-muxed photos
        self.input_dir: str = ""

    def start(self, trigger: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.trigger = trigger
            self.start_time = datetime.now().isoformat()
            self.lines.clear()
            self.line_total = 0
            self.cancel_requested = False
            self.stats = {"total": 0, "current": 0, "converted": 0, "skipped": 0, "errors": 0, "current_file": ""}
            self.skipped_files = []
            return True

    def read_from(self, cursor: int):
        """Lines at absolute index >= cursor that are still buffered, as
        [(index, line)], the next cursor, and a stats copy. A reader that fell
        behind the ring buffer silently skips to the oldest line kept."""
        with self._lock:
            first = self.line_total - len(self.lines)
            cursor = max(cursor, first)
            items = [(first + i, l) for i, l in enumerate(self.lines) if first + i >= cursor]
            return items, self.line_total, dict(self.stats)

    def _write_file_log(self, line: str):
        path = os.environ.get("LIVE2MOTION_LOG_FILE") or load_config().get("log_file")
        if not path:
            return
        try:
            if self._file_log is None or self._file_log_path != path:
                from logging.handlers import RotatingFileHandler
                lg = logging.getLogger("live2motion.run")
                lg.handlers.clear()
                lg.propagate = False
                lg.setLevel(logging.INFO)
                h = RotatingFileHandler(path, maxBytes=LOG_FILE_BYTES, backupCount=3, encoding="utf-8")
                h.setFormatter(logging.Formatter("%(message)s"))
                lg.addHandler(h)
                self._file_log, self._file_log_path = lg, path
            self._file_log.info(line)
        except OSError:
            self._file_log = None   # unwritable path: keep running without a file

    def emit(self, raw: str):
        with self._lock:
            self._emit(raw)

    def _emit(self, raw: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {raw}"
        self.lines.append(line)
        self.line_total += 1
        self._write_file_log(line)
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
            "line_count": self.line_total,
            "history": list(self.history),
        }


run_state = RunState()
scheduler = BackgroundScheduler()
watch_observer: Optional[Observer] = None


# ─── Config ───────────────────────────────────────────────────────────────────

# Keys the web UI may read/write via /api/config (name -> expected type).
CONFIG_TYPES = {k: type(v) for k, v in DEFAULT_CONFIG.items()}
MAX_STR_LEN = 4096

# Server-side settings that live only in config.json / environment and are never
# exposed or changed through the API: "host", "access_token", "browse_roots", "log_file".


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


def validate_config(data) -> dict:
    """Return a cleaned copy of a config submitted to /api/config, or raise
    ValueError with a user-facing message. Unknown keys are rejected."""
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object")
    unknown = sorted(set(data) - set(CONFIG_TYPES))
    if unknown:
        raise ValueError(f"Unknown config key(s): {', '.join(map(str, unknown))}")
    clean = {}
    for key, value in data.items():
        expected = CONFIG_TYPES[key]
        if expected is bool:
            if not isinstance(value, bool):
                raise ValueError(f"'{key}' must be true or false")
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 86400:
                raise ValueError(f"'{key}' must be a whole number between 0 and 86400")
        else:
            if not isinstance(value, str):
                raise ValueError(f"'{key}' must be a string")
            if len(value) > MAX_STR_LEN or "\0" in value:
                raise ValueError(f"'{key}' is too long or contains invalid characters")
        clean[key] = value
    if "video_removal_mode" in clean and clean["video_removal_mode"] not in video_removal.MODES:
        raise ValueError(f"'video_removal_mode' must be one of: {', '.join(video_removal.MODES)}")
    if clean.get("schedule_cron"):
        try:
            CronTrigger.from_crontab(clean["schedule_cron"])
        except ValueError:
            raise ValueError("'schedule_cron' is not a valid 5-field cron expression")
    return clean


# ─── Security: bind address, access token, browse roots ──────────────────────

IN_DOCKER = video_removal.in_docker()
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def get_host() -> str:
    """Address to listen on. Docker needs 0.0.0.0 (the published port is what
    limits exposure there); native runs default to loopback only."""
    if IN_DOCKER:
        return "0.0.0.0"
    return str(os.environ.get("LIVE2MOTION_HOST") or load_config().get("host") or "127.0.0.1")


def get_token() -> str:
    return str(os.environ.get("LIVE2MOTION_TOKEN") or load_config().get("access_token") or "")


class ApiDenied(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def check_origin(request: Request):
    """CSRF guard for state-changing requests: a browser always sends Origin on
    cross-site POST/DELETE, so it must match the Host we were addressed as."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin is None:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ApiDenied(403, "Cross-site request blocked")
        return   # non-browser clients (curl, scripts) send no Origin
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc.lower() != host.lower():
        raise ApiDenied(403, "Origin does not match host")


def check_token(request: Request):
    token = get_token()
    if not token:
        return
    supplied = request.headers.get("x-live2motion-token", "")
    # EventSource cannot set headers, so the progress stream also accepts a query param
    if not supplied and request.url.path == "/api/progress":
        supplied = request.query_params.get("token", "")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise ApiDenied(401, "Access token required")


def check_host_header(request: Request):
    """Stop DNS-rebinding when only reachable locally: a loopback-bound server
    only answers requests addressed to a loopback name."""
    if IN_DOCKER or get_host() not in LOOPBACK_HOSTS:
        return
    name = urlsplit("//" + request.headers.get("host", "")).hostname or ""
    if name not in LOOPBACK_HOSTS:
        raise ApiDenied(403, "Unexpected Host header")


def api_guard(request: Request):
    if request.url.path.startswith("/api/"):
        check_host_header(request)
        check_origin(request)
        check_token(request)


def _existing(path) -> Optional[Path]:
    try:
        p = Path(path).expanduser().resolve()
        return p if str(path).strip() and p.is_dir() else None
    except (OSError, ValueError, RuntimeError):
        return None


def browse_roots() -> list:
    """Directories /api/browse may enter (and everything below them). Configurable
    with LIVE2MOTION_BROWSE_ROOTS (os.pathsep separated) or "browse_roots" in
    config.json; by default the input/output folders, their parents up to two
    levels (never the filesystem root), and the home folder."""
    cfg = load_config()
    configured = os.environ.get("LIVE2MOTION_BROWSE_ROOTS", "").split(os.pathsep) \
        if os.environ.get("LIVE2MOTION_BROWSE_ROOTS") else cfg.get("browse_roots")
    if isinstance(configured, list) and configured:
        candidates = configured
    else:
        candidates = [] if IN_DOCKER else [Path.home()]
        if IN_DOCKER:
            candidates.append("/data")
        for key in ("input_directory", "output_directory"):
            p = _existing(cfg.get(key) or "")
            if p:
                candidates.append(p)
                for parent in list(p.parents)[:2]:
                    if parent != parent.parent:   # skip "/" and drive roots
                        candidates.append(parent)
    roots = []
    for c in candidates:
        p = _existing(c) if isinstance(c, (str, Path)) else None
        if p and p not in roots:
            roots.append(p)
    for drive in list_windows_drives():
        roots.append(Path(drive))
    return roots


def is_browsable(p: Path, roots: list) -> bool:
    return any(p == r or r in p.parents for r in roots)


PATH_CONFIG_KEYS = ("input_directory", "output_directory", "video_removal_folder")


def check_config_paths(clean: dict):
    """Raise ValueError if a folder setting points outside the browse roots, so a
    forged /api/config call cannot aim a run (or file removal) at e.g. /etc."""
    roots = browse_roots()
    base = clean.get("input_directory") or load_config().get("input_directory") or ""
    for key in PATH_CONFIG_KEYS:
        value = clean.get(key)
        if not value:
            continue
        p = Path(value).expanduser()
        if key == "video_removal_folder" and not p.is_absolute() and base:
            p = Path(base).expanduser() / p     # relative = below the input folder
        try:
            p = p.resolve()
        except (OSError, RuntimeError):
            raise ValueError(f"'{key}' is not a valid path")
        if not is_browsable(p, roots):
            raise ValueError(f"'{key}' is outside the allowed folders")


TOKEN_IN_QUERY = re.compile(r"([?&]token=)[^&\s\"']*", re.IGNORECASE)


class RedactTokenFilter(logging.Filter):
    """Keep ?token=... (used by the progress event stream) out of access logs."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(TOKEN_IN_QUERY.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = TOKEN_IN_QUERY.sub(r"\1***", record.msg)
        return True


def install_log_redaction():
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokenFilter) for f in access.filters):
        access.addFilter(RedactTokenFilter())


install_log_redaction()


def warn_if_exposed(host: str):
    if host in LOOPBACK_HOSTS or get_token():
        return
    # docker-compose tells us where it publishes the port; loopback there is safe
    if IN_DOCKER and os.environ.get("LIVE2MOTION_PUBLISH_ADDR") in LOOPBACK_HOSTS:
        return
    bar = "!" * 72
    print(f"\n{bar}\n SECURITY WARNING: listening on {host} with NO access token.\n"
          " Anyone who can reach this port can browse folders and delete videos.\n"
          " Set LIVE2MOTION_TOKEN (or \"access_token\" in config.json), or bind to\n"
          f" 127.0.0.1 / publish the Docker port on 127.0.0.1 only.\n{bar}\n",
          file=sys.stderr, flush=True)


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


def kill_process_tree(proc: subprocess.Popen, grace: float = 5.0):
    """Stop the MotionPhoto2 child and everything it started (notably its
    exiftool helper) so cancelling leaves no orphans."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        # /T = the whole tree; the child gets no chance to clean up on Windows
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
        return
    import signal
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)       # lets the muxer clean up its temp files
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)       # anything still in the group (exiftool)
    except ProcessLookupError:
        pass


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
        # utf-8 + errors="replace": the child forces UTF-8 output, and non-ASCII
        # (e.g. Windows) filenames must not abort the read loop.
        # Own process group/session so cancel can take down the whole tree.
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" \
            else {"start_new_session": True}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **group,
        )
        run_state.process = proc
        if run_state.cancel_requested:   # cancelled before the process existed
            kill_process_tree(proc)
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

TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", ".filepart", ".temp", ".icloud")


def is_temp_name(name: str) -> bool:
    """Partial/temporary files that sync clients (iCloud, OneDrive, browsers)
    and the muxer itself create next to real media; never worth a run."""
    low = name.lower()
    return (low.startswith((".", "~"))               # .IMG_1.HEIC.icloud, ~$x, .mp2tmp.* ...
            or low.endswith(TEMP_SUFFIXES)
            or Path(low).stem.endswith(".xmp"))       # muxer's IMG.XMP.heic intermediates


MAX_STABLE_WAIT = 1800   # give up waiting for a never-settling file after 30 min


class MediaHandler(FileSystemEventHandler):
    """Triggers a run once new/changed media has stopped changing: events are
    debounced, then every touched file must keep the same size and mtime for
    `stable_seconds` (so half-copied or still-syncing files are not processed)."""

    def __init__(self, debounce: int, skip_folder: Optional[Path] = None,
                 stable_seconds: int = 10, poll: float = 1.0, on_ready=None):
        self._skip_folder = skip_folder
        self._debounce = max(debounce, 10)
        self._stable = max(stable_seconds, 0)
        self._poll = poll
        self._on_ready = on_ready or (lambda: threading.Thread(target=do_run, args=("watch",), daemon=True).start())
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._pending: set = set()
        self._waiting = False

    def _relevant(self, path: str) -> bool:
        if self._skip_folder and video_removal.is_inside(path, self._skip_folder):
            return False   # videos moved aside by "move" mode must not retrigger a run
        p = Path(path)
        return p.suffix.lower() in WATCH_EXTS and not is_temp_name(p.name)

    def _touch(self, path: str):
        # Our own output/removals during a run are not new arrivals
        if run_state.running or not self._relevant(path):
            return
        with self._lock:
            self._pending.add(path)
        self._reset()

    def on_created(self, event):
        if not event.is_directory:
            self._touch(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._touch(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._touch(event.dest_path)

    def _reset(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            if self._waiting:
                return          # the active wait re-reads _pending, so it covers this event
            self._waiting = True
        try:
            ready = self.wait_until_stable()
        finally:
            with self._lock:
                self._waiting = False
        if ready:
            self._on_ready()

    def wait_until_stable(self) -> bool:
        """Block until all pending files are unchanged for `stable_seconds`.
        Returns True if at least one file is ready to process."""
        seen: dict = {}          # path -> ((size, mtime_ns), time first seen with that signature)
        deadline = time.monotonic() + MAX_STABLE_WAIT
        while True:
            with self._lock:
                paths = set(self._pending)
            now = time.monotonic()
            unstable, alive = False, 0
            for p in paths:
                try:
                    st = os.stat(p)
                except OSError:
                    with self._lock:
                        self._pending.discard(p)   # deleted or renamed away again
                    seen.pop(p, None)
                    continue
                alive += 1
                sig = (st.st_size, st.st_mtime_ns)
                if p not in seen or seen[p][0] != sig:
                    seen[p] = (sig, now)
                    unstable = unstable or self._stable > 0
                elif now - seen[p][1] < self._stable:
                    unstable = True
            if not unstable or now >= deadline:
                with self._lock:
                    self._pending.clear()
                return alive > 0
            time.sleep(self._poll)


def start_watcher(cfg: dict):
    global watch_observer
    stop_watcher()
    path = cfg.get("input_directory", "")
    if path and os.path.isdir(path):
        watch_observer = Observer()
        skip = video_removal.removal_folder(path, cfg.get("video_removal_folder"))
        watch_observer.schedule(MediaHandler(cfg.get("watch_debounce", 300), skip, cfg.get("watch_stable_seconds", 10)), path, recursive=True)
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
    install_log_redaction()   # uvicorn may have reset logging config after import
    scheduler.start()
    cfg = load_config()
    apply_schedule(cfg)
    if cfg.get("watch_enabled"):
        start_watcher(cfg)
    yield
    scheduler.shutdown(wait=False)
    stop_watcher()

app = FastAPI(title="Live2Motion Photos", lifespan=lifespan, dependencies=[Depends(api_guard)])


@app.exception_handler(ApiDenied)
async def api_denied_handler(request: Request, exc: ApiDenied):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.get("/")
def root():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    return {k: cfg[k] for k in CONFIG_TYPES}   # never expose host/access_token


@app.post("/api/config")
async def api_save_config(request: Request):
    try:
        cfg = validate_config(await request.json())
        check_config_paths(cfg)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # Keep server-side settings (host, access_token, browse_roots) the UI can't see
    cfg = {**load_config(), **cfg}
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
    if run_state.running:
        run_state.cancel_requested = True
        proc = run_state.process
        if proc:
            kill_process_tree(proc)
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
            items, next_cursor, stats = run_state.read_from(cursor)
            for index, line in items:
                yield f"data: {json.dumps({'line': line, 'index': index, 'stats': stats})}\n\n"
            cursor = next_cursor

            if run_state.running:
                was_running = True
            elif was_running and cursor >= run_state.line_total:
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
    roots = browse_roots()
    if not roots:
        return JSONResponse({"error": "No browsable folders configured"}, status_code=403)
    if path == THIS_PC:
        drives = list_windows_drives()
        if drives:
            return {"path": THIS_PC, "parent": None, "dirs": drives,
                    "sep": os.sep, "root_list": True}
        path = ""
    try:
        p = (Path(path) if path else roots[0]).resolve()   # resolves symlinks and ".."
    except Exception:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not p.exists() or not p.is_dir():
        # Saved config paths are sometimes POSIX-style (e.g. "/data" from the
        # Docker default) and don't exist as-is on Windows, where a leading
        # "/" resolves to the root of the current drive. Fall back to the
        # first allowed folder instead of leaving the browser dead-ended.
        if p != roots[0]:
            return api_browse(str(roots[0]))
        return JSONResponse({"error": "Not a directory"}, status_code=404)
    if not is_browsable(p, roots):
        return JSONResponse({"error": "Folder is outside the allowed locations"}, status_code=403)
    def is_accessible_dir(entry: Path) -> bool:
        try:
            # symlinks pointing outside the allowed roots are hidden, not followed
            return entry.is_dir() and is_browsable(entry.resolve(), roots)
        except (OSError, RuntimeError):
            return False   # e.g. macOS ~/.Trash has an ACL that blocks stat entirely

    # On Windows, a drive's parent is itself (there's no shared root across
    # drives) - send the user to the virtual drive list instead of a dead end.
    at_windows_drive_root = p == p.parent and sys.platform == "win32"
    parent = THIS_PC if at_windows_drive_root else (str(p.parent) if p != p.parent else None)
    if parent and parent != THIS_PC and not is_browsable(p.parent, roots):
        parent = None   # don't offer a way up out of the allowed roots

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
    the video is short enough to be a Live Photo clip AND the photo is confirmed to
    contain an identical copy of it — only then is the standalone video
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

    # Offer a video only if the photo really contains it: the embedded video must
    # equal the standalone one in size and SHA-256 (a ContentIdentifier match, or
    # a different clip that shares one, is not proof).
    muxed: set[tuple[str, str]] = set()
    for photo, vid in pairs:
        problem = video_removal.verify_embedded_video(photo, vid)
        if problem == "":
            muxed.add((photo, vid))

    videos, total_bytes, seen = [], 0, set()
    for photo, vid in pairs:
        if (photo, vid) in muxed and vid not in seen:
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


def find_available_port(preferred: int, host: str = "127.0.0.1", attempts: int = 50) -> int:
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
    host = get_host()
    port = find_available_port(DEFAULT_PORT, host)
    if port != DEFAULT_PORT:
        print(f"Port {DEFAULT_PORT} is already in use - using port {port} instead.")
    try:
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass
    print(f"Live2Motion Photos will be available at http://localhost:{port} (listening on {host})")
    warn_if_exposed(host)
    uvicorn.run("app:app", host=host, port=port, reload=False, log_level="info")
