# Microsoft Store packaging: build report (2026-11-09)

Branch `feat/msix-store`, based on `dev/integration` at 180559c (v1.1.0). Not pushed.
Worktree: `~/AI/Projects/Live2MotionPhotos/worktrees/feat-msix-store`. It is a worktree of the
persistent clone `~/AI/Projects/Live2MotionPhotos`; `worktrees/` is listed in that clone's
`.git/info/exclude`, and dev/integration was fetched into it as `refs/remotes/review/dev-integration`.

## What was built

| Part | Files |
|---|---|
| Desktop shell | `desktop/live2motion_desktop.py` |
| app.py hooks (no engine change) | `LIVE2MOTION_DATA_DIR`, `motionphoto2_cmd()` / `bin_entry_point()`, `LIVE2MOTION_EXCLUDED_DIRS` in `is_browsable()` |
| Build | `packaging/windows/build.ps1`, `live2motion.spec`, `make_assets.py`, `AppxManifest.template.xml` |
| Identity (single config spot) | `packaging/windows/store-identity.json` (real Partner Center values; DisplayName = reserved name "iPhone Live Photo to Android Motion Photo Converter Portal", used unshortened for Properties and VisualElements, since both allow 256 chars; tile ShortName "Live2Motion") |
| CI | `.github/workflows/store-msix.yml` (workflow_dispatch + `store-v*` tags) |
| Store kit | `store/listing.md`, `store/PRIVACY.md`, `store/SUBMISSION.md` |
| Tests | `tests/test_desktop.py` (14 tests) |

### Desktop shell behaviour
- pywebview 6.2.1 window (Edge WebView2, `gui="edgechromium"`). uvicorn runs in a thread on
  a socket bound to **127.0.0.1**: the preferred port is 47821 (so the window's local storage
  stays the same between starts), otherwise the OS picks a free port. No SO_REUSEADDR.
- **Token and Host checks on loopback (read from app.py):** a loopback bind normally needs no
  token (`token_required()` is False), and the Host allowlist accepts 127.0.0.1, localhost
  and ::1. The shell forces `LIVE2MOTION_HOST=127.0.0.1`, which overrides a `host` in
  config.json, and sets a **random per-start `LIVE2MOTION_TOKEN`**. The window opens
  `/?token=…`, and index.html stores the token and removes it from the URL. Other programs,
  and other Windows users on the same PC, therefore get 401. The Origin/CSRF and Host
  checks are unchanged.
- Single instance: named mutex `Local\MindThaApp.Live2MotionPhotos.Desktop`. A second start
  brings the first window to the front and exits with code 0.
- Closing the window: if a run is active, the app asks "Stop it and close?". If yes, it
  does what Cancel does (`kill_process_tree`). Then `server.should_exit`, the lifespan
  stops the scheduler and watcher, and the process exits.
- Data: `%LOCALAPPDATA%\Live2Motion\` holds config.json, preview.json, `logs\live2motion.log`
  (the existing rotating run log), `logs\desktop.log` (stdout/stderr, because the exe has
  no console) and `webview\`. The input folder is pre-filled by the existing
  `suggested_input_dir()` (the Pictures known folder), as before.
- Frozen converter: app.py runs `[Live2Motion.exe, --motionphoto2, …]`. The exe then runs
  the bundled `MotionPhoto2/motionphoto2.py` with runpy. ExifTool 13.59 ships in
  `exiftool\` next to the exe and is put first on PATH.

## Local build (this machine): MSIX built

Windows SDK 10.0.26100 (makeappx, makepri, signtool) and Python 3.12 were present.
Everything was written under `C:\AI\Projects\Live2MotionPhotos\store-build\` only:
`src\` (copy of the branch), `venv\`, `pip-cache\`, `tmp\`, and `out\run-<timestamp>\`.
Nothing was written to D:, and the dirty clone was not touched.

Final good run: `C:\AI\Projects\Live2MotionPhotos\store-build\out\run-20261109-185159\packages\`
- `34458MindtheApp.iPhoneLivePhototoAndroidMotionPhot_1.1.0.0_x64.msix` (32.7 MB, unsigned, for the Store)
- `…_x64.msixupload` (31.4 MB)
- `…_x64_sideload.msix` (signed with the test cert; subject `CN=FE63C3BB-418B-484C-852F-E7985F260BC3`)
- `sideload-test-cert.cer`. The test .pfx and its password are in `store-build\out\cert\`.
  They are reused by later local builds and are never committed.

Earlier `run-*` folders are failed or intermediate attempts. All of them are inside
store-build and can be deleted. `store-build\sample-test-1\` and `make_samples.py` are my
generated-sample test.

Smoke tests that passed on the frozen exe (they are part of build.ps1, so CI runs them too):
1. The bundled `exiftool -ver` returns 13.59. The SHA-256 of the zip is verified against
   the same pinned hash as portable-windows.yml.
2. `Live2Motion.exe --motionphoto2 --help` prints usage through a pipe. This proves the
   windowed exe's stdout reaches the parent.
3. `--smoke-test`: GET / returns 200, and `/api/status` without the token returns 401.
4. A Preview (dry run) through the server API on 2 generated JPG+MOV pairs: the exe
   started itself as the converter and found 2 planned pairs. The SHA-256 of every
   sample file was the same before and after.
5. `-GuiSmoke`: the real window opens and the server runs (checked on run-20261109-184937; the final run differs only in DisplayName).

Manual checks:
- A second instance exits with code 0 and the first keeps running.
- `CloseMainWindow()` leads to "Server stopped cleanly: True" and exit code 0.
- A real conversion with the frozen exe on generated samples (`--motionphoto2`, separate
  output folder) gave 2 converted, and the inputs were unchanged.

Not done locally: installing the MSIX. That needs the test cert in LocalMachine\Trusted People
(admin). The CI job does it on the runner: it installs the signed package, checks that the
family name equals `…_24e29cj9741rt`, starts the app from AppsFolder, checks `packaged=True`
plus "Serving on" in the **redirected** log, and uninstalls. The Windows App Certification
Kit was not run. The close-confirmation while a run is active, and the message shown when
WebView2 is missing, are untested.

## MSIX file-system virtualization (findings)

- Install folder (`WindowsApps\…`): read-only. That is why config and logs move to
  %LOCALAPPDATA%. MotionPhoto2's `save_defaults()` writes `motionphoto2.json` next to its
  script; in the package that write fails, and the code already ignores the error. It is
  harmless.
- **AppData** (`%LOCALAPPDATA%`, `%APPDATA%`, including `%TEMP%`, which is under
  LocalAppData): for a packaged full-trust app, new files are redirected to
  `%LOCALAPPDATA%\Packages\<PFN>\LocalCache\…` and **deleted on uninstall**. That is fine for
  our own config, logs and temp files. For user photos it would be data loss: an output
  folder or `_originals` folder created under AppData would disappear when the app is
  uninstalled. **Guard:** when packaged, the shell sets `LIVE2MOTION_EXCLUDED_DIRS` to the
  AppData folders. `is_browsable()` then refuses them for browsing and for every
  input/output/removal path, through `check_config_paths`, extract and run-selected.
  Tested in `test_excluded_dirs_*`.
- **Everything else is NOT virtualized:** Pictures, Documents, Desktop, OneDrive, other
  drives and the Recycle Bin. Converted photos, `_originals` moves and Recycle Bin moves
  therefore go to the real locations and survive uninstalling. The engine's temp files
  (`.mp2tmp.*`, `.mp2mv.*`) are created in the destination folder (real location), and
  `publish_no_overwrite` links or renames them there. app.py's `l2m_only_*.json` goes to
  %TEMP% (redirected) and is read by the child process, which runs in the same package
  context. That is fine.
- The registry is not used.
- Not verified hands-on: a packaged install on this PC. The CI step verifies the redirected
  log location (`…\Packages\<PFN>\LocalCache\Local\Live2Motion\logs\desktop.log`).

## Safety guards: unchanged

Engine files (`MotionPhoto2/*`) are not modified. app.py changes only:
- where config, preview and .port live;
- which command starts the converter (the existence check now looks at the exe when the
  last argument is a flag);
- an extra refusal in `is_browsable`.

The no-overwrite publish, safe_move, trash only on fixed drives, refusal when output equals
input, refusal on unreadable photos, the bin size check and the token/Host/Origin guards are
all untouched. The existing 319 tests still pass.

## Tests

- `pytest -q`: **333 passed** (319 existing + 14 new). `ruff check .`: all checks passed.

## Destructive-operation grep (whole diff 180559c..HEAD), each hit read in context

| Hit | Where | Verdict |
|---|---|---|
| `os.replace(log, logs/"desktop.log.1")` | desktop shell `setup_stdio` | OK: rotates the app's own log file in its own data folder, once it exceeds 5 MB. Replaces only the previous `.1`. |
| `open(log, "a")`, `open(…/"desktop.lock", "a")` | desktop shell | OK: append mode never truncates. Own files. |
| `open(os.devnull, "w")` | desktop shell | OK: the null device. |
| `open(port_file, "x")` | desktop `--smoke-test` | OK: exclusive create, so an existing file is never overwritten. |
| `cancel_active_run` → `kill_process_tree` | desktop close | OK: same code path as the existing Cancel button. |
| `open(path, "xb")` | make_assets.py; build.ps1 sample generator | OK: exclusive create, and it refuses a non-empty output folder. |
| `Rename-Item $part → exiftool zip` | build.ps1 cache | OK: our own download, renamed only after the SHA-256 check. Rename-Item fails if the target exists. |
| `Rename-Item exiftool(-k).exe` | build.ps1 | OK: inside our fresh dist copy. |
| `Rename-Item $zip → .msixupload` | build.ps1 | OK: a new file in the fresh run folder. |
| `Copy-Item … -Recurse`, `Expand-Archive` | build.ps1 | OK: destinations are new folders inside `run-<timestamp>`. |
| `PyInstaller --noconfirm` | build.ps1 | OK: dist and work paths are fresh run folders. **`--clean` was removed** because it wipes PyInstaller's global cache in %LOCALAPPDATA%\pyinstaller. My first local builds used `--clean`, so that regenerable cache folder no longer exists (it is only a build cache). PyInstaller's config dir is now kept inside the run folder. |
| `$pri.Save($PriCfg)`, `RemoveChild` | build.ps1 | OK: edits the priconfig.xml that makepri just created in the run folder. |
| `WriteAllText` manifest, password; `WriteAllBytes .cer` | build.ps1 | OK: new files. The password file is written only together with a newly created .pfx in our own cert folder. |
| `Remove-Item Cert:\CurrentUser\My\<thumb>` | build.ps1 | OK: removes only the self-signed cert this script created seconds earlier. The .pfx is kept. |
| `Remove-Item Env:\LIVE2MOTION_DATA_DIR` | build.ps1 | OK: an environment variable, not a file. |
| `Stop-Process -Id $srv.Id/$gui.Id` | build.ps1 | OK: only the smoke processes this script started. |
| `Get-Process Live2Motion \| Stop-Process`, `Remove-AppxPackage` | CI workflow | OK: runs only on the GitHub runner, on the package it just installed. |
| `"RUN_DIR=…" >> $env:GITHUB_ENV` | CI | OK: the standard GitHub env file. |
| `-replace "-"` / `.Replace("{{k}}")` | build.ps1 | OK: string operations. |

No hit touches user photos or videos. The only new file-system effect on user data is an
additional refusal (the AppData exclusion).

## What the user must do

1. Push: `git -C ~/AI/Projects/Live2MotionPhotos/worktrees/feat-msix-store push -u origin feat/msix-store`
   (the clone's origin URL is the old `ramin-azizi/Live2MotionPhotos.git`; GitHub redirects
   it, or set it to `MindThaApp/Live2MotionPhotos`). Then run the "Microsoft Store MSIX"
   workflow from Actions.
2. Optional: sideload-test the signed MSIX (store/SUBMISSION.md step 4), using copies of
   photos only.
3. Partner Center: the identity values are already in store-identity.json. Follow
   store/SUBMISSION.md for pricing, IARC rating, privacy URL (needs the branch merged to
   main, or GitHub Pages), screenshots (1920x1080 PNG, made yourself), runFullTrust
   justification, certification notes, and uploading the `.msixupload`.
4. Decide on the trademark risk in the name ("iPhone", "Android"). See SUBMISSION step 11.
