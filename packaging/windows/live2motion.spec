# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows desktop / Microsoft Store build (one-folder, windowed).
# Run through packaging/windows/build.ps1, which sets L2M_ICON to the generated app.ico.
import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
MP2 = os.path.join(ROOT, "MotionPhoto2")

hiddenimports = (
    ["app", "Muxer", "SamsungTags", "constants", "extract", "motionphoto2", "utils", "video_removal",
     "exiftool", "lxml", "lxml.etree", "send2trash", "six"]
    + collect_submodules("uvicorn")
    + collect_submodules("apscheduler")
    + collect_submodules("webview.platforms")
)

datas = [(os.path.join(ROOT, "index.html"), "."), (os.path.join(ROOT, "LICENSE"), ".")]
# motionphoto2.py is run as a script by `Live2Motion.exe --motionphoto2`, so the vendored
# sources ship as files too (their imports resolve to the same frozen modules).
datas += [(os.path.join(MP2, f), "MotionPhoto2") for f in sorted(os.listdir(MP2))
          if f.endswith(".py") or f in ("LICENSE", "README.md")]
datas += copy_metadata("apscheduler")

a = Analysis(
    [os.path.join(ROOT, "desktop", "live2motion_desktop.py")],
    pathex=[ROOT, MP2],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=["tkinter", "gooey", "wx", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Live2Motion",
    console=False,
    icon=os.environ.get("L2M_ICON") or None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Live2Motion", upx=False)
