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
import uuid
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

IN_DOCKER = video_removal.in_docker()


def windows_pictures_dir() -> Optional[Path]:
    """The real Pictures known folder on Windows (may be redirected to OneDrive)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]

        # FOLDERID_Pictures {33E28130-4E1E-4676-835A-98395C3BC3BB}
        fid = GUID(0x33E28130, 0x4E1E, 0x4676, (ctypes.c_ubyte * 8)(0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                  ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return None


def default_input_dir() -> str:
    """Docker: /data. Native: the user's Pictures folder, else the home folder."""
    if IN_DOCKER:
        return "/data"
    pics = windows_pictures_dir() or (Path.home() / "Pictures")
    try:
        if pics.is_dir():
            return str(pics)
    except OSError:
        pass
    return str(Path.home())


DEFAULT_CONFIG = {
    "input_directory": default_input_dir(),
    "output_directory": "",
    "recursive": True,
    "exif_match": True,
    "incremental_mode": False,
    "copy_unmuxed": False,
    "overwrite": False,
    "dry_run": False,             # list what would be converted/removed; write nothing
    "delete_video": False,
    "video_removal_mode": video_removal.default_mode(),   # shared: removed videos AND photos replaced by overwrite
    "video_removal_folder": "",   # "move" mode only (videos and replaced photos); empty = <input_directory>/_originals
    "extract_mov": False,         # Extract mode: save the video as .mov when that is safe
    "extract_still": False,       # Extract mode: also write a still image without the video
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
MAX_RESULTS = 100000         # per-file result rows kept for the results table
RESULT_PREFIX = "@@RESULT "    # machine-readable lines printed by motionphoto2.py
RESULT_STATUSES = {"converted", "skipped", "error", "planned"}
# Finer per-row classification the Preview filters on (see classify_row).
ROW_KINDS = ("ready", "motion_leftover", "motion_only", "no_mov", "no_photo", "duplicate_video", "skipped", "conflict", "error", "missing")


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
        self.results: deque = deque(maxlen=MAX_RESULTS)    # per-file outcomes (dicts)
        self.result_total = 0
        self.stats: dict = {"total": 0, "current": 0, "converted": 0, "skipped": 0, "errors": 0, "current_file": ""}
        self.history: list = []
        self.cancel_requested = False
        self.dry_run = False
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
            self.results.clear()
            self.result_total = 0
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

    def read_results(self, cursor: int):
        """Like read_from, for the per-file results: ([(index, result)], next cursor)."""
        with self._lock:
            first = self.result_total - len(self.results)
            cursor = max(cursor, first)
            return [(first + i, r) for i, r in enumerate(self.results) if first + i >= cursor], self.result_total

    def _add_result(self, raw: str):
        try:
            r = json.loads(raw[len(RESULT_PREFIX):])
        except ValueError:
            return
        if not isinstance(r, dict):
            return
        status = r.get("status")
        # Count from the per-file outcome, not the "Writing output file" log line: that
        # line comes before verification, so a photo that then failed was counted too.
        if status in ("converted", "planned"):
            self.stats["converted"] += 1
        self.results.append({
            "file": str(r.get("file", ""))[:1024],
            "status": status if status in RESULT_STATUSES else "error",
            "reason": str(r.get("reason", ""))[:1024],
            "output": str(r.get("output", ""))[:1024],
            "kind": r.get("kind") if r.get("kind") in ROW_KINDS else "",
            "video": str(r.get("video", ""))[:1024],
            "photo": str(r.get("photo", ""))[:1024],
            "bytes": r["bytes"] if isinstance(r.get("bytes"), int) and r["bytes"] > 0 else 0,
        })
        self.result_total += 1

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
        if raw.startswith(RESULT_PREFIX):     # goes to the results table, not the log
            self._add_result(raw)
            return
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
        elif "is not a motion photo, skipping" in rawl:
            self.stats["skipped"] += 1
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
                "dry_run": self.dry_run,
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
            if not str(cfg.get("input_directory") or "").strip():
                cfg["input_directory"] = default_input_dir()
            return cfg
        except Exception:
            pass
    return {**DEFAULT_CONFIG, "input_directory": default_input_dir()}


def save_config(cfg: dict):
    """Atomic write: a crash mid-save never leaves a truncated config.json."""
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def remember_folder(key: str, path: str) -> bool:
    """Persist a folder the user picked/used as input_directory/output_directory.
    Validated like /api/config; returns False (nothing saved) if it is not an
    existing, allowed folder."""
    if key not in ("input_directory", "output_directory") or not isinstance(path, str) \
            or not path.strip() or len(path) > MAX_STR_LEN or "\0" in path:
        return False
    p = _existing(path)
    if p is None:
        return False
    try:
        check_config_paths({key: str(p)})
    except ValueError:
        return False
    cfg = load_config()
    if cfg.get(key) != str(p):
        save_config({**cfg, key: str(p)})
    return True


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
    if clean.get("overwrite") and clean.get("output_directory", "").strip():
        raise ValueError("Overwrite and Output Directory cannot be used together: clear the output folder or untick overwrite")
    if clean.get("schedule_cron"):
        try:
            CronTrigger.from_crontab(clean["schedule_cron"])
        except ValueError:
            raise ValueError("'schedule_cron' is not a valid 5-field cron expression")
    return clean


# ─── Security: bind address, access token, browse roots ──────────────────────

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
    explicit = isinstance(configured, list) and bool(configured)
    if explicit:
        candidates = configured
    else:
        candidates = [] if IN_DOCKER else [Path.home()]
        if IN_DOCKER:
            candidates.append("/data")
        else:
            candidates.append(default_input_dir())   # Pictures may be redirected outside home (OneDrive)
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
    if not explicit:
        # Native Windows: the restriction exists for Docker / network exposure, so
        # every local drive stays browsable unless roots were configured explicitly.
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
    if cfg.get("delete_video") or cfg.get("overwrite"):
        cmd += ["--video-removal-mode", video_removal.normalize_mode(cfg.get("video_removal_mode"))]
        if cfg.get("video_removal_folder"):
            cmd += ["--video-removal-folder", cfg["video_removal_folder"]]
    if cfg.get("only_file"):
        cmd += ["--only-file", cfg["only_file"]]
    for key, flag in [
        ("recursive",       "--recursive"),
        ("exif_match",      "--exif-match"),
        ("incremental_mode","--incremental-mode"),
        ("copy_unmuxed",    "--copy-unmuxed"),
        ("overwrite",       "--overwrite"),
        ("delete_video",    "--delete-video"),
        ("dry_run",         "--dry-run"),
        ("keep_temp",       "--keep-temp"),
        ("no_xmp",          "--no-xmp"),
        ("verbose",         "--verbose"),
    ]:
        if cfg.get(key):
            cmd.append(flag)
    return cmd


def build_extract_cmd(cfg: dict) -> list:
    """Extract mode: split Motion Photos into still + video (MotionPhoto2 --extract)."""
    cmd = list(BIN) + ["--extract"]
    if f := cfg.get("input_file"):
        cmd += ["--input-image", f]
    elif d := cfg.get("input_directory"):
        cmd += ["--input-directory", d]
    if d := cfg.get("output_directory"):
        cmd += ["--output-directory", d]
    for key, flag in [
        ("recursive",     "--recursive"),
        ("extract_mov",   "--extract-mov"),
        ("extract_still", "--extract-still"),
        ("extract_force", "--extract-force"),
        ("dry_run",       "--dry-run"),
        ("verbose",       "--verbose"),
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


# ─── Preview (dry run) that can be acted on ──────────────────────────────────

PREVIEW_KEYS = ("input_directory", "output_directory", "recursive", "exif_match", "incremental_mode",
                "copy_unmuxed", "overwrite", "delete_video", "video_removal_mode",
                "video_removal_folder", "no_xmp")
last_preview: dict = {}      # {"id", "fingerprint", "names", "rows", "created", "done"} of the latest finished mux dry run
_preview_lock = threading.Lock()


def classify_row(r: dict) -> str:
    """The row's kind; rows without one (older Previews, plain runs) are derived from status/reason."""
    if r.get("kind") in ROW_KINDS:
        return r["kind"]
    st, why = r.get("status"), str(r.get("reason", "")).lower()
    if st == "planned":
        return "ready"
    if st == "error":
        return "error"
    if "already a motion photo" in why:
        return "motion_only"
    if "no matching video" in why:
        return "no_mov"
    if "would overwrite" in why:
        return "conflict"
    if "no matching photo" in why:
        return "no_photo"
    return "skipped"


def _norm_row(r: dict) -> dict:
    out = {k: str(r.get(k, "")) for k in ("file", "status", "reason", "output", "video", "photo")}
    out["kind"] = classify_row(r)
    b = r.get("bytes")
    out["bytes"] = b if isinstance(b, int) and not isinstance(b, bool) and b > 0 else 0
    return out


def _actionable(r: dict, done: set) -> bool:
    return (r["kind"] == "ready" and r["file"] not in done) or r["kind"] in ("motion_leftover", "duplicate_video")


def preview_file() -> Path:
    return CONFIG_FILE.with_name("preview.json")


def _save_preview():
    """Atomic write next to the config, so a restart keeps the Preview."""
    p = preview_file()
    data = {"id": last_preview["id"], "fingerprint": last_preview["fingerprint"],
            "created": last_preview["created"], "rows": last_preview["rows"],
            "done": sorted(last_preview["done"])}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def set_preview(fingerprint: dict, rows: list):
    with _preview_lock:
        last_preview.clear()
        last_preview.update({"id": uuid.uuid4().hex, "fingerprint": fingerprint, "created": time.time(),
                             "rows": rows, "names": {r["file"] for r in rows}, "done": set()})
        last_preview["rows"] = [_norm_row(r) for r in rows]
        try:
            _save_preview()
        except OSError:
            pass


def clear_preview():
    with _preview_lock:
        last_preview.clear()
        try:
            preview_file().unlink()
        except OSError:
            pass


def load_preview():
    """Restore the saved Preview after a restart (no-op if none or unreadable)."""
    try:
        d = json.loads(preview_file().read_text(encoding="utf-8"))
        rows = [_norm_row(r) for r in d["rows"]]
        with _preview_lock:
            last_preview.clear()
            last_preview.update({"id": str(d["id"]), "fingerprint": dict(d["fingerprint"]),
                                 "created": float(d["created"]), "rows": rows,
                                 "names": {r["file"] for r in rows}, "done": set(map(str, d.get("done", [])))})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass


def mark_preview_done(preview_id: str, converted):
    """Mark converted rows done; drop the Preview once nothing is left to convert."""
    with _preview_lock:
        if not last_preview or last_preview.get("id") != preview_id:
            return
        last_preview["done"] |= (set(converted) & last_preview["names"])
        rows = last_preview.get("rows")
        if rows:
            left = [r for r in rows if _actionable(r, last_preview["done"])]
        else:
            left = last_preview["names"] - last_preview["done"]
        if left:
            try:
                _save_preview()
            except OSError:
                pass
            return
    clear_preview()


def mark_preview_missing(preview_id: str, files):
    """A run reported these files as gone: the rows stop being selectable."""
    with _preview_lock:
        if not last_preview or last_preview.get("id") != preview_id:
            return
        files = set(files)
        for r in last_preview.get("rows", []):
            if r["file"] in files:
                r["kind"], r["video"] = "missing", ""
                r["reason"] = "file no longer exists"
        try:
            _save_preview()
        except (OSError, KeyError):
            pass


def preview_fingerprint(cfg: dict) -> dict:
    return {k: cfg.get(k) for k in PREVIEW_KEYS}


def preview_status() -> dict:
    """Latest preview, its rows, and whether settings that affect output changed since."""
    if not last_preview:
        load_preview()
    if not last_preview:
        return {"id": None, "stale": True, "count": 0, "rows": [], "created": None}
    stale = last_preview["fingerprint"] != preview_fingerprint(load_config())
    done = last_preview.get("done", set())
    with _preview_lock:
        rows = [{**r, "kind": classify_row(r), "done": r["file"] in done} for r in last_preview.get("rows", [])]
    root_dir = Path(load_config().get("input_directory") or "")
    for r in rows:
        r["state"] = "converted" if r["done"] else r["kind"]
        if r["kind"] in ("motion_leftover", "duplicate_video") and r.get("video"):
            r["bytes"] = video_removal.file_size(root_dir / r["video"])
    counts: dict = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    cfg = load_config()
    root = cfg.get("input_directory") or None
    return {"id": last_preview["id"], "stale": stale,
            "count": counts.get("ready", 0) if rows else len(last_preview["names"] - done),
            "rows": rows, "created": last_preview.get("created"),
            "counts": {"all": len(rows), **counts},
            "cleanup": {"mode": video_removal.normalize_mode(cfg.get("video_removal_mode")),
                        "destination": video_removal.describe_destination(
                            cfg.get("video_removal_mode"), root, cfg.get("video_removal_folder"))}}


def bin_applies(cfg: dict, action: str) -> bool:
    """Does this action send things to the Recycle Bin? (convert: only with overwrite / delete video)"""
    if video_removal.normalize_mode(cfg.get("video_removal_mode")) != "trash":
        return False
    return bool(cfg.get("delete_video") or cfg.get("overwrite")) if action == "convert" else True


def bin_load(files: list, rows: list, cfg: dict, action: str) -> tuple:
    """(bytes, items) these files would send to the bin, from the Preview rows."""
    rowmap = {r["file"]: r for r in rows}
    root = Path(cfg.get("input_directory") or "")
    total, items = 0, 0
    for f in dict.fromkeys(files):
        r = rowmap.get(f)
        if not r:
            continue
        if action == "convert":
            if classify_row(r) == "ready" and r.get("bytes"):
                total += r["bytes"]
                items += int(bool(cfg.get("overwrite"))) + int(bool(cfg.get("delete_video")))
        elif r.get("video"):
            total += video_removal.file_size(root / r["video"])
            items += 1
    return total, items


def bin_check(files: list, rows: list, cfg: dict, action: str) -> dict:
    """The guard's verdict for this action: {"applies": False} unless the bin would be used."""
    if not bin_applies(cfg, action):
        return {"applies": False, "ok": True}
    total, items = bin_load(files, rows, cfg, action)
    if not items:
        return {"applies": True, "ok": True, "estimate": 0, "files": 0, "cap": 0, "usage": None, "reason": ""}
    return {"applies": True, **video_removal.bin_report(total, items, cfg.get("input_directory") or ".")}


def bin_refusal(rep: dict) -> JSONResponse:
    return JSONResponse({"error": rep["reason"], "bin_guard": True, "bin": rep}, status_code=409)


def validate_selected(preview_id, files, cfg: dict, preview: dict, skipped: Optional[list] = None) -> list:
    """Return the cleaned list of relative image paths to convert, or raise ValueError.
    Every file must have been in the preview and sit inside the input directory (and
    the browse roots). Files that are gone or already converted are not an error:
    they are left out and, if `skipped` is given, appended to it as {"file", "reason"}."""
    if not preview or preview.get("id") != preview_id:
        raise ValueError("Preview not found: run Preview again")
    if preview["fingerprint"] != preview_fingerprint(cfg):
        raise ValueError("Preview is out of date: settings changed since the preview")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        raise ValueError("'files' must be a non-empty list of paths")
    root = Path(cfg.get("input_directory") or "").expanduser().resolve()
    if not root.is_dir() or not is_browsable(root, browse_roots()):
        raise ValueError("Input directory is not available")
    out = []
    done = preview.get("done", set())
    for f in dict.fromkeys(files):
        if f not in preview["names"]:
            raise ValueError(f"Not part of the preview: {f}")
        full = (root / f).resolve()
        if not video_removal.is_inside(full, root) or not is_browsable(full, browse_roots()):
            raise ValueError(f"Path is outside the input directory: {f}")
        kind = next((classify_row(r) for r in preview.get("rows", []) if r["file"] == f), "ready")
        if f in done:
            if skipped is not None:
                skipped.append({"file": f, "reason": "already converted"})
        elif kind != "ready":
            if skipped is not None:
                skipped.append({"file": f, "reason": "file no longer exists" if kind == "missing"
                                else f"not ready to convert ({kind})"})
        elif not full.is_file():
            if skipped is not None:
                skipped.append({"file": f, "reason": "file no longer exists"})
        else:
            out.append(f)
    return out


def do_run(trigger: str = "manual", dry_run: Optional[bool] = None, extract: Optional[dict] = None,
           only_files: Optional[list] = None, preview_id: Optional[str] = None,
           skipped_files: Optional[list] = None):
    """extract: None for the normal mux run, else the per-request overrides for an Extract run."""
    if not run_state.start(trigger):
        return
    cfg = load_config()
    if extract is not None:
        cfg.update(extract)
    if dry_run is not None:
        cfg["dry_run"] = dry_run      # per-request override of the config toggle
    only_path = None
    if only_files and extract is None:
        import tempfile
        fd, only_path = tempfile.mkstemp(prefix="l2m_only_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(only_files, fh)
        cfg["only_file"] = only_path
    run_state.dry_run = bool(cfg.get("dry_run"))
    run_state.input_dir = "" if extract is not None else cfg.get("input_directory", "")
    cmd = build_extract_cmd(cfg) if extract is not None else build_cmd(cfg)
    run_state.emit(f"▶ Triggered by: {trigger}" + (" (DRY RUN - nothing will be changed)" if run_state.dry_run else ""))
    run_state.emit("$ " + " ".join(cmd))
    for sk in skipped_files or []:
        run_state.emit(f"Skipped {sk['file']}: {sk['reason']}")
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
        if proc.returncode == 0 and run_state.dry_run and extract is None and not only_files:
            rows = [r for _, r in run_state.read_results(0)[0]]
            set_preview(preview_fingerprint(cfg), rows)
        elif proc.returncode == 0 and only_files and preview_id:
            mark_preview_done(preview_id, [r["file"] for _, r in run_state.read_results(0)[0]
                                           if r["status"] == "converted"])
        run_state.finish(proc.returncode)
    except Exception as exc:
        run_state.emit(f"✗ Exception: {exc}")
        run_state.finish(-1)
    finally:
        if only_path:
            try:
                os.remove(only_path)
            except OSError:
                pass


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

    def __init__(self, debounce: int, skip_folder=None,
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
        skips = self._skip_folder if isinstance(self._skip_folder, (list, tuple)) else [self._skip_folder]
        if any(f and video_removal.is_inside(path, f) for f in skips):
            return False   # videos moved aside by "move" mode must not retrigger a run
        p = Path(path)
        if "_extracted" in p.parts:
            return False   # Extract mode output, never input for the converter
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
        skip = video_removal.skip_folders(path, cfg.get("video_removal_folder"))
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
    out = {k: cfg[k] for k in CONFIG_TYPES}   # never expose host/access_token
    out["docker"] = IN_DOCKER                 # the UI explains that Recycle Bin falls back to a folder there
    # folders that no longer exist (drive unplugged, renamed...): the UI shows a warning
    out["missing_paths"] = [k for k in ("input_directory", "output_directory")
                            if cfg.get(k) and _existing(cfg[k]) is None]
    return out


@app.post("/api/last-folder")
async def api_last_folder(request: Request):
    """Remember a folder picked in the browser dialog (no Save press needed)."""
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not remember_folder(data.get("key"), data.get("path")):
        return JSONResponse({"error": "Not an existing, allowed folder"}, status_code=400)
    return {"ok": True}


@app.post("/api/config")
async def api_save_config(request: Request):
    try:
        cfg = validate_config(await request.json())
        check_config_paths(cfg)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # Keep server-side settings (host, access_token, browse_roots) the UI can't see
    cfg = {**load_config(), **cfg}
    if cfg.get("overwrite") and str(cfg.get("output_directory") or "").strip():
        return JSONResponse({"error": "Overwrite and Output Directory cannot be used together: "
                                      "clear the output folder or untick overwrite"}, status_code=400)
    save_config(cfg)
    apply_schedule(cfg)
    if cfg.get("watch_enabled"):
        start_watcher(cfg)
    else:
        stop_watcher()
    return {"ok": True}


@app.get("/api/preview")
def api_preview():
    """Latest finished Preview: id, item count, and whether settings changed since."""
    return preview_status()


@app.post("/api/run-selected")
async def api_run_selected(request: Request):
    """Convert exactly the ticked files of the latest Preview, without rescanning.
    Body: {"preview_id": "...", "files": ["rel/path.jpg", ...]}."""
    if run_state.running:
        return JSONResponse({"error": "Already running"}, status_code=409)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        cfg = load_config()
        if not last_preview:
            load_preview()
        skipped: list = []
        files = validate_selected(body.get("preview_id"), body.get("files"), cfg, last_preview, skipped)
    except ValueError as e:
        msg = str(e)
        return JSONResponse({"error": msg, "stale": "Preview" in msg or "changed" in msg},
                            status_code=409 if "Preview" in msg or "changed" in msg else 400)
    mark_preview_missing(body["preview_id"], [s["file"] for s in skipped if s["reason"] == "file no longer exists"])
    if files:
        rep = bin_check(files, last_preview.get("rows", []), cfg, "convert")
        if not rep["ok"]:
            return bin_refusal(rep)
        threading.Thread(target=do_run, args=("manual", False, None, files, body["preview_id"], skipped),
                         daemon=True).start()
    return {"ok": True, "count": len(files), "skipped": skipped}


@app.post("/api/cleanup-movs")
async def api_cleanup_movs(request: Request):
    """Dispose of the leftover MOVs of Preview rows classed 'already a Motion Photo'.
    Body: {"preview_id": "...", "files": [photo rel paths]}. Each MOV is removed only
    after the photo is re-verified to contain an identical embedded video; the
    configured removal mode applies (bin by default, move to folder, or delete)."""
    if run_state.running:
        return JSONResponse({"error": "Already running"}, status_code=409)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        cfg = load_config()
        if not last_preview:
            load_preview()
        files = body.get("files")
        pv = last_preview
        if not pv or pv.get("id") != body.get("preview_id"):
            raise ValueError("Preview not found: run Preview again")
        if pv["fingerprint"] != preview_fingerprint(cfg):
            raise ValueError("Preview is out of date: settings changed since the preview")
        if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
            raise ValueError("'files' must be a non-empty list of paths")
        root = Path(cfg.get("input_directory") or "").expanduser().resolve()
        if not root.is_dir() or not is_browsable(root, browse_roots()):
            raise ValueError("Input directory is not available")
        rowmap = {r["file"]: r for r in pv.get("rows", [])}
        for f in files:
            if f not in rowmap:
                raise ValueError(f"Not part of the preview: {f}")
    except ValueError as e:
        msg = str(e)
        stale = "Preview" in msg or "changed" in msg
        return JSONResponse({"error": msg, "stale": stale}, status_code=409 if stale else 400)
    rep = bin_check(files, pv.get("rows", []), cfg, "cleanup")
    if not rep["ok"]:
        return bin_refusal(rep)
    return cleanup_movs(files, rowmap, cfg, root)


@app.post("/api/bin-check")
async def api_bin_check(request: Request):
    """Would these ticked Preview rows fit the Recycle Bin? Body: {"files": [...], "action": "convert"|"cleanup"}."""
    try:
        body = await request.json()
        files, action = body.get("files"), body.get("action")
        if action not in ("convert", "cleanup") or not isinstance(files, list) \
                or not all(isinstance(f, str) for f in files):
            raise ValueError("bad request")
    except (ValueError, AttributeError):
        return JSONResponse({"error": "Body must be {files: [...], action: convert|cleanup}"}, status_code=400)
    if not last_preview:
        load_preview()
    return bin_check(files, last_preview.get("rows", []) if last_preview else [], load_config(), action)


def _after_cleanup(row: dict, kind: str):
    if kind == "duplicate_video":
        row["kind"], row["reason"] = "skipped", "duplicate video removed (verified identical to the video inside the photo)"
    else:
        row["kind"], row["video"], row["reason"] = "motion_only", "", "already a motion photo"


def cleanup_movs(files: list, rowmap: dict, cfg: dict, root: Path) -> dict:
    cleaned, refused, errors, skipped = [], [], [], []
    mode = cfg.get("video_removal_mode")
    gone = []
    for f in dict.fromkeys(files):
        row = rowmap[f]
        kind = classify_row(row)
        if kind not in ("motion_leftover", "duplicate_video") or not row.get("video") \
                or (kind == "duplicate_video" and not row.get("photo")):
            skipped.append({"file": f, "reason": "no leftover MOV for this row"})
            continue
        # A duplicate video's row is the video itself; the photo it must be verified against is stored beside it
        photo = (root / (row["photo"] if kind == "duplicate_video" else f)).resolve()
        video = (root / row["video"]).resolve()
        if not (video_removal.is_inside(photo, root) and video_removal.is_inside(video, root)
                and is_browsable(photo, browse_roots()) and is_browsable(video, browse_roots())):
            errors.append({"file": f, "error": "path is outside the input directory"})
            continue
        if not photo.is_file():
            gone.append(f)
            skipped.append({"file": f, "reason": "photo no longer exists"})
            continue
        if not video.is_file():
            with _preview_lock:
                _after_cleanup(row, kind)
            skipped.append({"file": f, "reason": "MOV already gone"})
            continue
        problem = video_removal.verify_embedded_video(str(photo), str(video))
        if problem:
            refused.append({"file": f, "reason": problem})
            continue
        try:
            action = video_removal.remove_video(str(video), mode, str(root), cfg.get("video_removal_folder"))
        except Exception as e:
            errors.append({"file": f, "error": str(e)})
            continue
        cleaned.append({"file": f, "video": row["video"], "action": action})
        with _preview_lock:
            _after_cleanup(row, kind)
    if gone:
        with _preview_lock:
            for f in gone:
                rowmap[f].update(kind="missing", video="", reason="file no longer exists")
    with _preview_lock:
        try:
            _save_preview()
        except (OSError, KeyError):
            pass
    return {"ok": True, "cleaned": len(cleaned), "details": cleaned, "refused": refused,
            "errors": errors, "skipped": skipped}


@app.post("/api/run")
def api_run(dry_run: Optional[bool] = None):
    """dry_run=true|false overrides the saved "dry_run" setting for this run."""
    if run_state.running:
        return JSONResponse({"error": "Already running"}, status_code=409)
    threading.Thread(target=do_run, args=("manual", dry_run), daemon=True).start()
    return {"ok": True}


EXTRACT_KEYS = {"input_directory": str, "input_file": str, "output_directory": str, "recursive": bool,
                "extract_mov": bool, "extract_still": bool, "extract_force": bool, "dry_run": bool}


def validate_extract_request(data) -> dict:
    """Clean per-request overrides for /api/extract (ValueError with a user-facing
    message). Every path must be inside the browse roots, like the saved settings."""
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    unknown = sorted(set(data) - set(EXTRACT_KEYS))
    if unknown:
        raise ValueError(f"Unknown key(s): {', '.join(map(str, unknown))}")
    for key, value in data.items():
        if not isinstance(value, EXTRACT_KEYS[key]):
            raise ValueError(f"'{key}' must be a {'string' if EXTRACT_KEYS[key] is str else 'true/false value'}")
        if EXTRACT_KEYS[key] is str and (len(value) > MAX_STR_LEN or "\0" in value):
            raise ValueError(f"'{key}' is too long or contains invalid characters")
    check_config_paths({k: v for k, v in data.items() if k in PATH_CONFIG_KEYS})
    if f := data.get("input_file"):
        try:
            p = Path(f).expanduser().resolve()
        except (OSError, RuntimeError):
            raise ValueError("'input_file' is not a valid path")
        if not is_browsable(p, browse_roots()):
            raise ValueError("'input_file' is outside the allowed folders")
        if not p.is_file():
            raise ValueError("'input_file' is not a file")
        data = {**data, "input_file": str(p)}
    return data


@app.post("/api/extract")
async def api_extract(request: Request, dry_run: Optional[bool] = None):
    """Extract mode: split Motion Photos into a still image and a video file.
    Optional JSON body overrides the saved settings for this run (input_directory,
    input_file, output_directory, recursive, extract_mov, extract_still, dry_run);
    ?dry_run=true|false overrides both."""
    if run_state.running:
        return JSONResponse({"error": "Already running"}, status_code=409)
    try:
        raw = await request.body()
        overrides = validate_extract_request(json.loads(raw) if raw.strip() else {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    for key in ("input_directory", "output_directory"):
        if overrides.get(key):
            remember_folder(key, overrides[key])
    if dry_run is None:
        dry_run = overrides.pop("dry_run", None)
    else:
        overrides.pop("dry_run", None)
    # The saved settings must also point inside the browse roots (a hand-edited config.json might not)
    try:
        check_config_paths({k: v for k, v in load_config().items()
                                  if k in ("input_directory", "output_directory") and k not in overrides})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    threading.Thread(target=do_run, args=("extract", dry_run, overrides), daemon=True).start()
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


@app.get("/api/results")
def api_results():
    """Per-file outcomes of the current/last run (the table in the UI)."""
    items, total = run_state.read_results(0)
    return {"results": [r for _, r in items], "total": total}


@app.get("/api/progress")
async def api_progress(request: Request, from_line: int = 0):
    """SSE stream — delivers all lines for the current (or next) run."""
    async def generate():
        yield "retry: 2000\n\n"
        cursor = from_line
        rcursor = 0
        was_running = False

        while True:
            if await request.is_disconnected():
                break

            # Drain any new lines
            items, next_cursor, stats = run_state.read_from(cursor)
            for index, line in items:
                yield f"data: {json.dumps({'line': line, 'index': index, 'stats': stats})}\n\n"
            cursor = next_cursor
            results, rcursor = run_state.read_results(rcursor)
            for index, result in results:
                yield f"data: {json.dumps({'result': result, 'rindex': index})}\n\n"

            if run_state.running:
                was_running = True
            elif was_running and cursor >= run_state.line_total and rcursor >= run_state.result_total:
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


def _browse_start(roots: list) -> Path:
    """Where the folder dialog opens: the current/last-used input folder, else
    the default folder, else the first allowed root."""
    cfg = load_config()
    for cand in (cfg.get("input_directory"), default_input_dir()):
        p = _existing(cand or "")
        if p and is_browsable(p, roots):
            return p
    return roots[0]


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
    start = _browse_start(roots)
    try:
        p = (Path(path) if path else start).resolve()   # resolves symlinks and ".."
    except Exception:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        is_dir = p.is_dir()
    except OSError:   # e.g. PermissionError on protected Windows folders
        return JSONResponse({"error": "Permission denied"}, status_code=403)
    if not is_dir:
        # Saved config paths are sometimes POSIX-style (e.g. "/data" from the
        # Docker default) and don't exist as-is on Windows, where a leading
        # "/" resolves to the root of the current drive. Fall back to the
        # first allowed folder instead of leaving the browser dead-ended.
        if p != start:
            return api_browse(str(start))
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
    skip = {str(f) for f in video_removal.skip_folders(input_dir, load_config().get("video_removal_folder"))}
    for root, _dirs, files in os.walk(input_dir, followlinks=True):
        _dirs[:] = [d for d in _dirs if str(Path(root, d).resolve()) not in skip]   # don't rescan removed videos
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
