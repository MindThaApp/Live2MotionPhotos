# Changelog

## Unreleased

### Data safety
- Cancel no longer kills the converter mid-file on Windows. Cancel (all platforms) and
  closing the desktop app now ask the converter to stop after the file it is working on, so
  replacing an original (set aside, then put the merged photo in place) is never cut in
  half. The converter tells the app when it is inside one file's work, and it is never
  force-stopped then, however long the file takes (e.g. a multi-GB move to a slow NAS). A
  stuck converter is force-stopped only after 10 minutes with no progress at all
  (`LIVE2MOTION_CANCEL_TIMEOUT`, seconds), never after a fixed time.
- "Clean up MOVs" now stops between two files on Cancel or when the app is closing; each
  video's copy, verify and delete still completes. The rest is reported as not processed.
- A symlinked photo or video that leads into an excluded folder (Store build: AppData, temp,
  install folder) is skipped and left alone.
- The Store build never excludes a drive root (C:\\), a relative path, or the current folder
  that Python falls back to when no temp folder is usable; skipped entries are logged.
- Every run (manual, schedule, folder watcher, Extract) re-checks the saved input, output
  and removal folders before it starts. This is stricter than before: on Linux/macOS (not
  Docker) a schedule or watcher run whose output or removal folder is not under the home,
  Pictures or input folders, or whose input folder is missing right now (e.g. an unmounted
  drive), is now refused with "Refusing to run" where it used to run. Re-save the settings
  with allowed folders, or mount the drive.
- The Microsoft Store build also refuses the temp and install folders, and a recursive scan
  never enters an excluded folder (e.g. AppData when the input is the home folder).

## v1.1.0 (2026-11-09)

**Upgrade now. v1.0.5 and every earlier release (v1.0.0 to v1.0.5) can permanently delete or overwrite your photos and videos. Do not use them.**

### Data safety
- Original photos are never overwritten unless you opt in, and then only after a verified backup exists.
- Original videos are moved (not deleted) by default.
- Videos shared by more than one photo are protected and never removed.
- If any photo cannot be read, video removal stops for that run.
- Output folder equal to the input folder is refused.
- On drives without a Recycle Bin, Recycle Bin mode is refused instead of silently deleting.
- Output files use collision-safe names, so existing files are never replaced.

### Security
- An access token is now required when the app is reachable over the network or in Docker.
- Protection against DNS-rebinding attacks.
- The unsafe cleanup endpoint was removed.
- File-name injection is blocked.
