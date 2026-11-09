# Changelog

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
