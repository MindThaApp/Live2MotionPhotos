# Privacy Policy

**App:** iPhone Live Photo to Android Motion Photo (also called Live2Motion Photos)
**Publisher:** MindtheApp
**Last updated:** 9 November 2026

## Summary

The app works only on your computer. It does not collect, store, send or share any
personal information. It has no accounts, no ads, no analytics and no tracking.

## What the app does with your files

- It reads the photos and videos in the folders you choose so it can pair and convert them.
- It writes converted photos to the folder you choose. If you turn on the optional
  tidy-up or replace settings, it moves originals to an "_originals" folder or to the
  Windows Recycle Bin.
- Your photos and videos never leave your computer.

## What the app stores on your computer

In the app's own folder (`%LOCALAPPDATA%\Live2Motion`, which Windows keeps inside the
app's private storage):

- your settings, such as the folders you chose (`config.json`);
- the last Preview result, a list of file names and what will happen to them (`preview.json`);
- log files that record which files were converted (`logs\`);
- the app window's own browser storage (`webview\`).

This data stays on your computer. Uninstalling the app removes it.

## Network use

- **No internet connections.** The app does not contact any server: no update checks,
  no usage statistics, no crash reports and no downloads.
- **A local-only connection inside your computer.** The app's window talks to the app's
  converter through a connection on `127.0.0.1` (your own computer). It cannot be reached
  from other computers. A new random access key is created each time the app starts, so
  other programs and other user accounts on the same computer cannot use it either.
- **Microsoft Edge WebView2.** The app's window is drawn by Microsoft Edge WebView2, a part
  of Windows. Windows keeps WebView2 up to date on its own, and WebView2 follows your
  Windows diagnostic and privacy settings. Microsoft's privacy statement covers this:
  https://privacy.microsoft.com/privacystatement
- **Microsoft Store.** Installing and updating the app through the Microsoft Store is
  handled by Windows and the Store under Microsoft's privacy statement.

## Children

The app collects no information from anyone, including children.

## Changes

If this policy changes, the new version will be published at the same address with a new
"Last updated" date.

## Contact

Questions: open an issue at https://github.com/MindThaApp/Live2MotionPhotos/issues
