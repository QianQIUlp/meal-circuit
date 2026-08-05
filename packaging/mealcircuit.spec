# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys


root = Path(SPECPATH).parent
target_arch = os.environ.get("MEALCIRCUIT_TARGET_ARCH") if sys.platform == "darwin" else None
use_upx = sys.platform == "win32"
windows_icon = str(root / "packaging" / "windows" / "MealCircuit.ico") if sys.platform == "win32" else None
keyring_hiddenimports = ["keyring.backends.fail"]
platform_excludes = []
if sys.platform == "win32":
    keyring_hiddenimports.append("keyring.backends.Windows")
    platform_excludes.extend(
        [
            "webview.platforms.android",
            "webview.platforms.cocoa",
            "webview.platforms.gtk",
            "webview.platforms.qt",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.libsecret",
            "keyring.backends.kwallet",
        ]
    )
elif sys.platform == "darwin":
    keyring_hiddenimports.append("keyring.backends.macOS")
else:
    keyring_hiddenimports.extend(
        [
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
            "keyring.backends.chainer",
        ]
    )
datas = [
    (str(root / "mealcircuit" / "static"), "mealcircuit/static"),
    (str(root / "rules"), "rules"),
    (str(root / "templates"), "templates"),
    (str(root / "protocol"), "protocol"),
]

a = Analysis(
    [str(root / "mealcircuit" / "desktop.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "webview",
        *keyring_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=platform_excludes,
    noarchive=False,
    optimize=1,
)
if sys.platform == "win32":
    a.datas = [
        item
        for item in a.datas
        if not str(item[0]).replace("\\", "/").endswith("webview/lib/pywebview-android.jar")
    ]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MealCircuit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    console=False,
    target_arch=target_arch,
    icon=windows_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=use_upx,
    upx_exclude=[],
    name="MealCircuit",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MealCircuit.app",
        bundle_identifier="org.mealcircuit.desktop",
        target_arch=target_arch,
        info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"},
    )
