# Live2Motion Photos

A web UI for converting iPhone Live Photos into Google / Samsung Motion Photos, so they animate natively in Google Photos on Android. Works two ways: run it **locally** on your own Windows, macOS, or Linux machine and use it entirely by yourself (open `http://localhost:7000` in a browser on that same computer — nothing else required), or **self-host** it on a home server so it's reachable from any device on your network. Same app either way; see [Installation](#installation) for both.

Built as a browser-based frontend around [MotionPhoto2](https://github.com/PetrVys/MotionPhoto2) by PetrVys, adding a real-time progress dashboard, folder browser, scheduled runs, and file-watcher automation.

MotionPhoto2's source is vendored in `MotionPhoto2/` (pinned to upstream commit `5848f9b`) and run directly via `python3` — there is no compiled binary to build or download. This repo carries one small patch on top of upstream: a missing paired file (e.g. an image whose video got deleted, or a duplicate `ContentIdentifier` collision) is logged and skipped instead of calling `sys.exit(1)` and killing the whole batch. See `MotionPhoto2/Muxer.py` (`MuxerInputError`) and `MotionPhoto2/motionphoto2.py` (`failed_pairs`) for the diff from upstream.

---

## Why

When you transfer iPhone Live Photos to Android or a PC, they split into two separate files — a still image (`.HEIC` / `.JPEG`) and a short video clip (`.MOV`). Google Photos on Android does not automatically recognise these as a pair and will not animate them.

Live2Motion wraps MotionPhoto2 to mux each pair into a single Google Motion Photo file (`.LIVE.HEIC` or in-place overwrite), which Google Photos recognises and animates exactly as Apple's Live Photos do on iPhone.

---

## Features

- **Browser UI** — works purely on your own machine (`localhost:7000`) for solo local use, or accessible from any device on your network if self-hosted; no app to install on your phone either way
- **Real-time progress** — live terminal output with per-file status, colour-coded warnings and errors
- **Statistics panel** — donut chart showing Completed / Skipped / Errors / Remaining with live updates
- **Folder browser** — point-and-click navigation of the server filesystem to select input/output directories
- **All MotionPhoto2 settings** exposed with hover tooltips explaining each option
- **Scheduled runs** — built-in cron scheduler with presets (hourly, daily, weekly) or custom cron expressions
- **File watcher** — monitors the input directory and auto-triggers a run when new media arrives, with configurable debounce delay
- **Run history** — last 20 runs with trigger type, duration, converted/skipped/error counts, and exit status
- **Persistent settings** — configuration saved to `config.json` and restored on restart
- **Systemd service** — runs in the background and starts on boot

---

## Requirements

- Linux, macOS, or Windows (tested natively on Debian 13 and macOS; Windows works but is less battle-tested — Docker is the more proven path there, see below)
- Python 3.9+ (native install only — not needed for Docker or the portable Windows ZIP, which bundles its own Python)
- [ExifTool](https://exiftool.org/) (`sudo apt install libimage-exiftool-perl` on Linux, `brew install exiftool` on macOS, [exiftool.org](https://exiftool.org/) build on Windows — native install only, not needed for Docker or the portable Windows ZIP, which bundles its own ExifTool)

The web UI itself is just a browser page — once the server (native or Docker) is running anywhere on your network, you open it from **any** device's browser, Windows included, with nothing to install on the client side.

---

## Installation

Three ways to run it: **Docker** (no Python/ExifTool install needed, identical on every OS — recommended for Windows and macOS), **portable** (Windows ZIP, fully offline), or **native** (recommended for Linux, e.g. as a systemd service on a homelab server).

### Option A: Docker (Linux, macOS, Windows)

Requires [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on macOS/Windows, Docker Engine on Linux).

```bash
git clone https://github.com/ramin-azizi/Live2MotionPhotos.git
cd Live2MotionPhotos
cp config.example.json config.json
```

No `docker-compose.yml` editing needed — just double-click:

- **`Start Live2Motion (Docker).command`** (macOS)
- **`Start Live2Motion (Docker).bat`** (Windows)
- **`start-live2motion-docker.sh`** (Linux)

The first run asks which photos folder to share (default `~/Pictures`); it is written to a local `.env` file as `HOST_MEDIA` together with your user/group ID, and mounted at `/data` in the container. Only that folder is visible to the app — your whole home folder is no longer mounted. The launcher then builds the image, starts it in the background, and opens **http://localhost:7000**. Use the in-app folder browser to pick the exact folder under `/data`.

To use a different photos folder later, edit `HOST_MEDIA` in `.env` (an `.env` written by an older launcher may still point at your home folder — change it).

**Permissions.** The container runs as a non-root user whose UID/GID come from `LIVE2MOTION_UID` / `LIVE2MOTION_GID` in `.env` (default 1000; the Linux/macOS launchers write your own `id -u` / `id -g`). That user must be able to write to `HOST_MEDIA` and to `config.json`: if you start with `docker compose` by hand, add those two lines to `.env` (`id -u`, `id -g`), or `chown`/`chmod` the folder for the UID you chose. On Windows/macOS Docker Desktop handles file ownership for you.

**Network access.** The port is published on `127.0.0.1` only, so only the machine running Docker can open the UI. To reach it from other devices, set `LIVE2MOTION_BIND=0.0.0.0` in `.env` and also set `LIVE2MOTION_TOKEN` (see [Security](#security)).

The container keeps running in the background after you close the launcher window (`restart: unless-stopped`). To stop it: `docker compose down`. To update after pulling new code: rerun the launcher (or `docker compose up -d --build`).

### Option A2: Portable (Windows, fully offline) — easiest, no technical knowledge needed

Nothing to install — no Python, no Docker, no admin rights, no internet needed after the download:

1. Download **`Live2Motion-Portable-Windows.zip`** from the [Releases page](https://github.com/ramin-azizi/Live2MotionPhotos/releases/latest).
2. Right-click the downloaded ZIP → **Extract All…** and pick any location (your Desktop or even a USB stick is fine).
3. Open the extracted folder and double-click **`Start Live2Motion (Portable).bat`**. A black window opens (that's the app running) and your browser opens the app at http://localhost:7000 a few seconds later.
4. In the browser, pick the folder with your Live Photos and click Start.

Everything — the Python runtime, dependencies, and ExifTool — lives inside the extracted folder; nothing is installed on your system. To stop the app, close the black window. To uninstall, delete the folder.

> If Windows shows a "Windows protected your PC" SmartScreen warning the first time, click **More info → Run anyway** — it appears because the file is new/unsigned, not because anything is wrong.

### Option B: Native (Linux / macOS / Windows)

**Easiest — for running on your own computer:** get the code, either with `git clone https://github.com/ramin-azizi/Live2MotionPhotos.git`, or via GitHub's **Code → Download ZIP** button (no `git` required) — then just double-click:

- **`Start Live2Motion.command`** (macOS)
- **`Start Live2Motion.bat`** (Windows)
- **`start-live2motion.sh`** (Linux)

That's the whole install. First run creates the Python virtual environment, installs dependencies, creates `config.json`, and tries to install ExifTool automatically if it's missing (Homebrew on macOS, apt/dnf on Linux, winget on Windows) — then opens the app in your browser. Every run after that just starts the server and reopens the browser tab. Requires Python 3.9+ to already be installed ([python.org/downloads](https://www.python.org/downloads/)) — the script checks for it and tells you if it's missing. If ExifTool can't be auto-installed on your system, the script prints the exact manual steps.

<details>
<summary><b>Manual setup</b> (for a headless server, or if you'd rather control each step yourself)</summary>

#### 1. Install ExifTool

```bash
sudo apt install libimage-exiftool-perl   # Linux
brew install exiftool                     # macOS
```
On Windows, download the ExifTool executable from [exiftool.org](https://exiftool.org/), rename it to `exiftool.exe`, and put it somewhere on your `PATH`.

#### 2. Clone and install Live2Motion

```bash
git clone https://github.com/ramin-azizi/Live2MotionPhotos.git
cd Live2MotionPhotos
python3 -m venv venv
venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip install -r requirements.txt
cp config.example.json config.json            # Windows: copy config.example.json config.json
```

This installs both the web app's dependencies and MotionPhoto2's (the vendored `MotionPhoto2/` folder is run directly as a subprocess — no separate download or build step).

#### 3. Run

```bash
venv/bin/python app.py      # Windows: venv\Scripts\python app.py
```

Then open **http://localhost:7000** in a browser. To reach it from other devices as **http://your-server-ip:7000**, set `LIVE2MOTION_HOST=0.0.0.0` and `LIVE2MOTION_TOKEN` first (see [Security](#security)). (port 7000 may already be taken by macOS Control Center on a Mac — use a different port for local dev, see below).

</details>

### Local development on a machine other than the homelab server

You don't need the server or its photo library to work on the FastAPI routes / UI. Run it on any free port:

```bash
venv/bin/python3 -c "
import uvicorn, app
uvicorn.run(app.app, host='127.0.0.1', port=7099)
"
```

The input folder defaults to your Pictures folder (`/data` in Docker) and the app remembers the last folder you picked or ran. To point at a different (or fake) photo library, edit `input_directory` in `config.json`. To use a pre-built binary instead of the vendored source (e.g. for speed), set `MOTIONPHOTO2_BIN=/path/to/binary` in the environment before starting.

---

## Running as a systemd service (auto-start on boot, Linux only)

For Docker on any OS, `restart: unless-stopped` in `docker-compose.yml` already handles auto-start/restart — no separate service setup needed. On Windows running natively, use Task Scheduler (run `app.py` "at log on", or as a service via [NSSM](https://nssm.cc/)) instead of the systemd unit below.

```bash
sudo nano /etc/systemd/system/live2motion.service
```

```ini
[Unit]
Description=Live2Motion Photos Web UI
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/Live2MotionPhotos
ExecStart=/path/to/Live2MotionPhotos/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now live2motion
```

---

## Usage

### Settings

| Setting | Flag | Description |
|---|---|---|
| Input Directory | | Root folder containing your Live Photos |
| Output Directory | | Where to save results. Leave empty to use Overwrite mode |
| Recursive | `-r` | Scan all subdirectories |
| EXIF Match | `-em` | Match pairs by EXIF metadata instead of filename. Recommended for iPhone exports where filenames may not align |
| Incremental Mode | `-im` | Skip files already converted. Requires an Output Directory |
| Copy Unmuxed | `-cu` | Copy non-live photos unchanged to the Output Directory |
| Overwrite Originals | `-o` | Replace source images in place. No Output Directory needed |
| Delete Video After Mux | `-dv` | Remove the `.MOV` file after embedding it (only after the output is verified; see below) |
| Keep Temp Files | `-kt` | Retain temporary files for debugging |
| Skip XMP Metadata | `-nx` | Do not copy XMP tags to the output |
| Verbose | `-v` | Detailed per-file log output |

### Video removal

When **Delete Video After Mux** is on, the output is checked first (it exists, is at least image + video in size, and contains the exact video bytes). If the check fails the `.MOV` is kept and a warning is logged. Otherwise the video is removed according to `video_removal_mode`:

| Mode | Behaviour |
|---|---|
| `trash` | Send to the OS recycle bin (default outside Docker). If no bin is available it falls back to `move`, never to `delete` |
| `move` | Move into `video_removal_folder` (default `_deleted_videos` inside the input directory, default in Docker), keeping the relative path. Name clashes get a numeric suffix. The folder is ignored by scans and the file watcher |
| `delete` | Permanently delete |

The Cleanup tool uses the same mode and only accepts video files inside the input directory. Configs that only have `delete_video` keep working and get the default mode.

### Extract (Motion Photo to photo + video)

The reverse direction: split an existing Motion Photo into a still image and a separate video. Supports Google Motion Photo v1, legacy MicroVideo and the Samsung trailer format. The video is cut out byte for byte (no re-encoding, no ffmpeg) and must start with an MP4 `ftyp` box; if the offsets in the metadata are wrong, the file is scanned for one.

- UI: the **Extract Video** section in the sidebar (Preview = dry run). CLI: `motionphoto2.py --extract --input-directory DIR` (or `--input-image FILE`), plus `--output-directory`, `--extract-mov`, `--extract-still`, `--dry-run`. API: `POST /api/extract[?dry_run=true]`.
- Output: `NAME.mp4` in an `_extracted` folder next to each photo, or in the Output Directory (the converter and watcher never scan `_extracted`, so the video is not muxed back into the photo). `--extract-mov` names it `.mov` only when the container can be renamed unchanged (plain isom/QuickTime file), otherwise it stays `.mp4`. `--extract-still` also writes `NAME_still.jpg` (JPEG only) without the video and, if exiftool is installed, without the Motion Photo XMP flags.
- With exiftool installed, the capture date and GPS are copied onto the video; without it that step is skipped.
- Originals are never modified and existing files are never overwritten (a taken name becomes `NAME_1.mp4`). Photos whose `NAME.mp4`/`.mov` already exists in the output folder are skipped on re-run; `--extract-force` extracts again under a new suffix.

### Schedule

Enable **scheduled runs** and choose a preset (hourly / daily 2 AM / weekly Sunday 2 AM) or enter a custom [cron expression](https://crontab.guru/). The next scheduled run time is shown below the selector.

### File Watcher

Enable **auto-run on new files** to monitor the Input Directory for incoming `.HEIC`, `.JPEG`, `.MOV`, or `.MP4` files. After new files stop arriving for the configured **debounce period** (default 300 s), a conversion run is triggered automatically. Set the debounce higher than the time it takes to fully transfer a batch of photos from your iPhone.

---

## Limitations

### HDR in Google Photos

HDR is displayed correctly in Google Photos only for HEIC files with HDR stored in ISO/CD 21496-1 format — effectively requiring **iPhone 15+ shooting on iOS 18+**.

For photos from older devices (iPhone 14 and earlier, iPads, etc.), Google Photos will report the photo as non-HDR after upload. This is not accurate — the Apple HDR gain map is preserved in the file untouched by Live2Motion. If you save the photo back to iPhone Camera Roll, the native Photos app will render it as HDR. Google Photos on iPhone/iPad will also display it correctly, as the iOS app reads Apple's gain map directly, bypassing the server-side HDR flag.

The reason is that Google's server-side pipeline stops checking for HDR metadata once it encounters the GCamera XMP object (which Motion Photos require). This is a known limitation of MotionPhoto2 itself:

> *"It appears that the server-side processing of Google Photos does not check for Apple HDR or ISO HDR once it finds Google Camera header in XMP tags."*
> — [MotionPhoto2 README](https://github.com/PetrVys/MotionPhoto2/#limitations)

For JPG files a metadata-only fix is theoretically possible (adjusting the JPEG/R HDR fields without re-encoding). For HEIF files, converting Apple's gain map to ISO `tmap` format is non-trivial and not yet implemented in any open-source tool.

**In practice:** for family photo sharing viewed primarily on iPhones, the HDR is fully preserved and visible. The limitation only affects Android displays or Google Photos on the web.

### Skipped file detection

Skip detection parses the MotionPhoto2 log output for keywords (`skip`, `already`, `no matching`). It may undercount in edge cases where the tool silently skips a file without logging a recognisable message.

### Security

By default the server listens on `127.0.0.1` (this computer only); in Docker the port is published on `127.0.0.1` only. To make it reachable on your network:

- **Native:** set `LIVE2MOTION_HOST=0.0.0.0` (or `"host": "0.0.0.0"` in `config.json`).
- **Docker:** set `LIVE2MOTION_BIND=0.0.0.0` in `.env`.

When you do, also set an access token, via `LIVE2MOTION_TOKEN` or `"access_token"` in `config.json`. Every `/api` request must then carry it; the web UI asks for it once and remembers it in the browser. The app prints a warning at startup if it is reachable beyond localhost without a token. With no token and localhost-only, there is no prompt.

Other protections: state-changing requests whose `Origin` doesn't match the `Host` are rejected (CSRF); the folder browser only enters allowed roots (home, the input/output folders and their parents up to two levels, Windows drives; override with `LIVE2MOTION_BROWSE_ROOTS` or `"browse_roots": [...]` in `config.json`) and does not follow symlinks out of them; `/api/config` validates every field; the Docker container runs as non-root. `host`, `access_token` and `browse_roots` can only be set in `config.json`/environment, never through the web UI.

The token travels in plain HTTP on your LAN, so this is for trusted networks. Do not expose port 7000 to the internet; put a TLS reverse proxy in front if you need that.

Development: `pip install -r requirements-dev.txt && pytest`.

---

## Credits

Live2Motion Photos is a web frontend that orchestrates [MotionPhoto2](https://github.com/PetrVys/MotionPhoto2). The actual image/video muxing, EXIF matching, and Motion Photo format implementation are entirely the work of that project and its contributors.

### MotionPhoto2

- **[PetrVys](https://github.com/PetrVys)** — original author and Motion Photo v2/v3 format research
- **[Tkd-Alex](https://github.com/Tkd-Alex)** — ported the original PowerShell script to Python
- **[NightMean](https://github.com/NightMean)** — EXIF metadata matching
- **[sahilph](https://github.com/sahilph)** — copy of non-live photos in directory mode
- **[tribut](https://github.com/tribut), [4Urban](https://github.com/4Urban), [IamRysing](https://github.com/IamRysing)** — sample Motion Photo contributions

### References

- [Google Motion Photo Format](https://developer.android.com/media/platform/motion-photo-format)
- [Samsung Motion Photo trailer tags](https://github.com/doodspav/motionphoto) by doodspav
- [ExifTool](https://exiftool.org/) by Phil Harvey

### Live2Motion web stack

- [FastAPI](https://fastapi.tiangolo.com/) — web framework
- [APScheduler](https://apscheduler.readthedocs.io/) — cron scheduling
- [watchdog](https://python-watchdog.readthedocs.io/) — filesystem monitoring
- [uvicorn](https://www.uvicorn.org/) — ASGI server

---

## License

MIT — see [LICENSE](LICENSE).
