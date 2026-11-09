#!/usr/bin/env python3
"""Live2Motion Photos desktop shell (Windows desktop / Microsoft Store build).

Runs the existing FastAPI app (app.py) in-process on 127.0.0.1 and shows it in a native
window (pywebview + Microsoft Edge WebView2) instead of a browser tab.

- Listens on loopback only, on a free port (a fixed preferred port first, so the
  window's local storage survives restarts).
- A random access token is generated for every start and handed to the window, so
  other programs or other Windows users on this computer cannot drive the API.
- One instance at a time: a second start brings the existing window to the front.
- Closing the window stops a running conversion (after asking) and shuts the server down.
- Settings, logs and the window's storage live in %LOCALAPPDATA%\\Live2Motion. Inside an
  MSIX package Windows redirects that folder into the package's private storage.

The frozen exe also serves as the converter process: app.py starts
`Live2Motion.exe --motionphoto2 <args>`, which runs MotionPhoto2/motionphoto2.py.

This module never touches photos or videos itself: all conversion, removal and
no-overwrite rules stay in app.py and MotionPhoto2/ unchanged.
"""

import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "Live2Motion"
WINDOW_TITLE = "Live2Motion Photos"
PREFERRED_PORT = 47821
MUTEX_NAME = "Local\\MindThaApp.Live2MotionPhotos.Desktop"
DESKTOP_LOG_MAX_BYTES = 5 * 1024 * 1024
APPMODEL_ERROR_NO_PACKAGE = 15700
ERROR_ALREADY_EXISTS = 183


# ─── Locations ──────────────────────────────────────────────────────────────────

def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Folder holding app.py's files (index.html, MotionPhoto2/): the PyInstaller bundle
    when frozen, else the repository root."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def install_dir() -> Path:
    """Folder of the exe (frozen) or the repository root: where exiftool\\ is shipped."""
    return Path(sys.executable).parent if frozen() else bundle_dir()


def data_dir(env=None) -> Path:
    """%LOCALAPPDATA%\\Live2Motion (LIVE2MOTION_DATA_DIR overrides it)."""
    env = os.environ if env is None else env
    if env.get("LIVE2MOTION_DATA_DIR"):
        return Path(env["LIVE2MOTION_DATA_DIR"])
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIR_NAME


def appdata_dirs(env=None) -> list:
    """The per-user AppData folders. New files a packaged (MSIX) app creates there are
    redirected into the package's private storage and deleted on uninstall, so the
    packaged app refuses them as input/output folders."""
    env = os.environ if env is None else env
    out = []
    for c in (env.get("LOCALAPPDATA"), env.get("APPDATA"), str(Path.home() / "AppData")):
        if c and c not in out:
            out.append(c)
    return out


def is_packaged() -> bool:
    """True when running with MSIX package identity."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        length = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        return rc != APPMODEL_ERROR_NO_PACKAGE
    except Exception:
        return False


def configure_environment(data: Path, token: str, packaged: bool, env=None) -> None:
    """Point app.py at the data folder, loopback and the session token, and put the
    bundled ExifTool first on PATH. Must run before app.py is imported."""
    env = os.environ if env is None else env
    env["LIVE2MOTION_DATA_DIR"] = str(data)
    env["LIVE2MOTION_HOST"] = "127.0.0.1"     # also overrides a "host" in config.json
    env["LIVE2MOTION_TOKEN"] = token
    env.setdefault("LIVE2MOTION_LOG_FILE", str(data / "logs" / "live2motion.log"))
    exiftool_dir = install_dir() / "exiftool"
    if (exiftool_dir / "exiftool.exe").is_file() or (exiftool_dir / "exiftool").is_file():
        env["PATH"] = str(exiftool_dir) + os.pathsep + env.get("PATH", "")
    if packaged:
        extra = [d for d in env.get("LIVE2MOTION_EXCLUDED_DIRS", "").split(os.pathsep) if d]
        env["LIVE2MOTION_EXCLUDED_DIRS"] = os.pathsep.join(extra + appdata_dirs(env))


# ─── Converter subprocess mode ──────────────────────────────────────────────────

def run_motionphoto2(args: list) -> int:
    """`Live2Motion.exe --motionphoto2 ...`: run the vendored converter as this process."""
    import runpy
    script = bundle_dir() / "MotionPhoto2" / "motionphoto2.py"
    if sys.stdout is None or sys.stderr is None:   # windowed exe started without pipes
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull
    sys.argv = [str(script)] + list(args)
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


# ─── Logging (windowed exe has no console) ──────────────────────────────────────

def setup_stdio(logs: Path) -> None:
    """A windowed exe has sys.stdout/stderr = None, which breaks print() and uvicorn's
    logging. Send both to logs\\desktop.log (append; the app's own log, rotated once at 5 MB)."""
    if sys.stdout is not None and sys.stderr is not None and not frozen():
        return
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "desktop.log"
    try:
        if log.is_file() and log.stat().st_size > DESKTOP_LOG_MAX_BYTES:
            os.replace(log, logs / "desktop.log.1")    # only ever our own log file
    except OSError:
        pass
    try:
        f = open(log, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = f
    sys.stderr = f


# ─── Single instance ────────────────────────────────────────────────────────────

class SingleInstance:
    """Windows: a named mutex. Elsewhere (tests, dev): an exclusive lock on a lock file."""

    def __init__(self, data: Path, name: str = MUTEX_NAME):
        self.data, self.name = data, name
        self._handle = None
        self._fh = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            self._handle = kernel32.CreateMutexW(None, False, self.name)
            return bool(self._handle) and kernel32.GetLastError() != ERROR_ALREADY_EXISTS
        import fcntl
        self.data.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.data / "desktop.lock", "a")   # append: never truncates anything
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._handle:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
        if self._fh:
            self._fh.close()
            self._fh = None


def focus_existing_window() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def message_box(text: str, title: str = WINDOW_TITLE, yes_no: bool = False) -> bool:
    """Native message box (Windows); True for OK/Yes. Elsewhere it prints and says yes."""
    if sys.platform == "win32":
        try:
            import ctypes
            flags = (0x04 | 0x30) if yes_no else 0x10   # MB_YESNO|MB_ICONWARNING / MB_ICONERROR
            rc = ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x00010000)  # MB_SETFOREGROUND
            return rc in (1, 6)   # IDOK, IDYES
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr, flush=True)
    return True


# ─── Server ─────────────────────────────────────────────────────────────────────

def bind_loopback_socket(preferred: int = PREFERRED_PORT) -> socket.socket:
    """A listening-ready socket on 127.0.0.1: the preferred port if free, else any free
    port. No SO_REUSEADDR: on Windows it would let us share a port another app uses."""
    for port in (preferred, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return sock
        except OSError:
            sock.close()
    raise RuntimeError("No free port on 127.0.0.1")


def start_server(asgi_app, sock: socket.socket):
    """Run uvicorn on `sock` in a background thread. Returns (server, thread)."""
    import uvicorn
    config = uvicorn.Config(asgi_app, loop="asyncio", http="h11", ws="none", lifespan="on",
                            log_level="info", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              name="live2motion-server", daemon=True)
    thread.start()
    return server, thread


def wait_started(server, thread: threading.Thread, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.05)
    return False


def cancel_active_run(appmod) -> bool:
    """Same as the Cancel button (/api/cancel). True if a run was cancelled."""
    state = appmod.run_state
    if not state.running:
        return False
    state.cancel_requested = True
    proc = state.process
    if proc:
        appmod.kill_process_tree(proc)
    return True


def stop_server(server, thread: threading.Thread, appmod, timeout: float = 15.0) -> bool:
    cancel_active_run(appmod)
    server.should_exit = True
    thread.join(timeout)
    return not thread.is_alive()


# ─── Window ─────────────────────────────────────────────────────────────────────

def run_window(url: str, appmod, data: Path) -> None:
    import webview

    window = webview.create_window(WINDOW_TITLE, url, width=1240, height=880,
                                   min_size=(900, 600), text_select=True)

    def on_closing():
        if appmod.run_state.running:
            if not message_box("A conversion is still running.\n\nStop it and close Live2Motion Photos?",
                               yes_no=True):
                return False
            cancel_active_run(appmod)
        return True

    window.events.closing += on_closing
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(data / "webview"))


# ─── Entry point ────────────────────────────────────────────────────────────────

def _arg_value(argv: list, flag: str) -> Optional[str]:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--motionphoto2"]:
        return run_motionphoto2(argv[1:])

    # --smoke-test: start the server without a window (CI). With --port-file, the port and
    # this session's token are written as JSON to that NEW file (never overwrites one).
    smoke = "--smoke-test" in argv
    port_file = _arg_value(argv, "--port-file")

    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    setup_stdio(data / "logs")
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} Live2Motion desktop starting (data: {data})", flush=True)

    instance = SingleInstance(data)
    if not smoke and not instance.acquire():
        focus_existing_window()
        return 0

    try:
        packaged = is_packaged()
        token = secrets.token_urlsafe(32)
        configure_environment(data, token, packaged)
        print(f"packaged={packaged} frozen={frozen()} bundle={bundle_dir()}", flush=True)

        if not frozen() and str(bundle_dir()) not in sys.path:
            sys.path.insert(0, str(bundle_dir()))
        import app as appmod   # after configure_environment: reads LIVE2MOTION_DATA_DIR at import
        if appmod.config_problem():
            message_box(f"Live2Motion Photos cannot start: {appmod.config_problem()}.\n\n"
                        "The file was not changed. Fix or move it, then start the app again.")
            return 1

        sock = bind_loopback_socket()
        port = sock.getsockname()[1]
        server, thread = start_server(appmod.app, sock)
        if not wait_started(server, thread):
            message_box("Live2Motion Photos could not start its local server. See the log in\n"
                        f"{data / 'logs' / 'desktop.log'}")
            return 1
        print(f"Serving on http://127.0.0.1:{port}", flush=True)

        if smoke:
            if port_file:
                import json
                with open(port_file, "x", encoding="utf-8") as f:
                    json.dump({"port": port, "token": token}, f)
            thread.join()
            return 0

        try:
            run_window(f"http://127.0.0.1:{port}/?token={token}", appmod, data)
        except Exception as e:
            print(f"Window failed: {e!r}", flush=True)
            message_box("Live2Motion Photos needs the Microsoft Edge WebView2 Runtime, which could not be "
                        "started.\n\nInstall it from https://go.microsoft.com/fwlink/p/?LinkId=2124703 "
                        f"and try again.\n\nDetails: {e}")
        stopped = stop_server(server, thread, appmod)
        print(f"Server stopped cleanly: {stopped}", flush=True)
        return 0
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
