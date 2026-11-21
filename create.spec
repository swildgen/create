# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Create.app (same shape as Summon's)."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# VERSION is the single source of truth: the app reads the same file at runtime.
APP_VERSION = Path("VERSION").read_text(encoding="utf-8").strip()

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=[("ui.html", "."), ("VERSION", ".")] + collect_data_files("pymobiledevice3"),
    hiddenimports=["offload", "updater", "webview", "xxhash", "rich"] + collect_submodules("pymobiledevice3"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="create", debug=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="create")
app = BUNDLE(
    coll,
    name="Create.app",
    icon="assets/create.icns",
    bundle_identifier="com.wildgen.create",
    info_plist={
        "CFBundleName": "Create",
        "CFBundleDisplayName": "Create",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
