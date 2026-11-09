"""Cooperative cancel for the converter.

The app asks the converter to stop by creating the file named in LIVE2MOTION_CANCEL_FILE
(works the same on Windows, where there is no SIGTERM for a windowed child). The
converter only looks at it at safe points, between files (checkpoint()). Work on one
file (convert, set the original aside, put the merged photo in place, remove the video,
copy, extract) runs inside critical(): a stop request never lands in the middle of it.

SIGTERM (POSIX) keeps working: outside critical() it stops the run at once, as before;
inside it the request is remembered and the run stops at the next checkpoint.
"""
import os
import signal
import sys
from contextlib import contextmanager

CANCEL_FILE_ENV = "LIVE2MOTION_CANCEL_FILE"
CANCELLED_EXIT = 4
CANCELLED_MESSAGE = "[CANCELLED] Stopped on request between two files. The last file was finished; nothing was left half done."

_state = {"depth": 0, "pending": False}


def requested() -> bool:
    if _state["pending"]:
        return True
    path = os.environ.get(CANCEL_FILE_ENV)
    return bool(path) and os.path.exists(path)


def checkpoint() -> None:
    """A safe point (between files): stop the run here if a stop was requested."""
    if _state["depth"] == 0 and requested():
        print(CANCELLED_MESSAGE, flush=True)
        sys.exit(CANCELLED_EXIT)


@contextmanager
def critical():
    """One file's work: a stop request waits until it is done."""
    _state["depth"] += 1
    try:
        yield
    finally:
        _state["depth"] -= 1


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
