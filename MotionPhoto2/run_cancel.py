"""Cooperative cancel for the converter.

The app asks the converter to stop by creating the file named in LIVE2MOTION_CANCEL_FILE
(works the same on Windows, where there is no SIGTERM for a windowed child). The
converter only looks at it at safe points, between files (checkpoint()). Work on one
file (convert, set the original aside, put the merged photo in place, remove the video,
copy, extract) runs inside critical(): a stop request never lands in the middle of it.

SIGTERM (POSIX) keeps working: outside critical() it stops the run at once, as before;
inside it the request is remembered and the run stops at the next checkpoint.

Status file (LIVE2MOTION_STATUS_FILE, optional): the converter tells the app whether it is
inside one file's work. On entering critical() it writes "busy", on leaving it "idle"; each
checkpoint() refreshes "idle" (that is also a sign of progress). The app's last-resort
watchdog never force-stops the converter while the file says "busy", and otherwise only
after a long time with no progress (see app.py, _cancel_watchdog). The file belongs to the
app (a unique temp file per run); the converter only rewrites its content.
"""
import os
import signal
import sys
import time
from contextlib import contextmanager

CANCEL_FILE_ENV = "LIVE2MOTION_CANCEL_FILE"
STATUS_FILE_ENV = "LIVE2MOTION_STATUS_FILE"
BUSY, IDLE = "busy", "idle"
STATUS_FILE_PREFIX = "l2m_status_"     # the app names it l2m_status_<pid>_<uuid>.txt
CANCELLED_EXIT = 4
CANCELLED_MESSAGE = "[CANCELLED] Stopped on request between two files. The last file was finished; nothing was left half done."

_state = {"depth": 0, "pending": False, "status": None, "status_at": 0.0}


def _set_status(status: str) -> None:
    """Tell the app (status file) whether a file is being worked on. Best effort: a status
    that cannot be written only means the app falls back to output-based progress."""
    path = os.environ.get(STATUS_FILE_ENV)
    if not path or not os.path.basename(path).startswith(STATUS_FILE_PREFIX):
        return                          # only ever rewrite the app's own status file, never another file
    now = time.monotonic()
    if status == _state["status"] and status == IDLE and now - _state["status_at"] < 1.0:
        return                          # idle refreshes at most once a second
    try:
        with open(path, "w", encoding="ascii") as fh:     # the app's own per-run temp file
            fh.write(f"{status} {os.getpid()}\n")
    except OSError:
        return
    _state["status"], _state["status_at"] = status, now


def requested() -> bool:
    if _state["pending"]:
        return True
    path = os.environ.get(CANCEL_FILE_ENV)
    return bool(path) and os.path.exists(path)


def checkpoint() -> None:
    """A safe point (between files): stop the run here if a stop was requested."""
    if _state["depth"] != 0:
        return
    _set_status(IDLE)
    if requested():
        print(CANCELLED_MESSAGE, flush=True)
        sys.exit(CANCELLED_EXIT)


@contextmanager
def critical():
    """One file's work: a stop request waits until it is done."""
    _state["depth"] += 1
    if _state["depth"] == 1:
        _set_status(BUSY)
    try:
        yield
    finally:
        _state["depth"] -= 1
        if _state["depth"] == 0:
            _set_status(IDLE)


def on_sigterm(signum=None, frame=None):
    _state["pending"] = True
    if _state["depth"] == 0:
        sys.exit(1)        # between files: stop now (temp files are removed on the way out)
    # inside one file's work: finish it, checkpoint() stops the run afterwards


def install_sigterm_handler() -> None:
    signal.signal(signal.SIGTERM, on_sigterm)


def reset() -> None:
    """Tests only."""
    _state["depth"] = 0
    _state["pending"] = False
    _state["status"] = None
    _state["status_at"] = 0.0
