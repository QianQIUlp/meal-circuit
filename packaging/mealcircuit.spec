# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys


root = Path(SPECPATH).parent
target_arch = os.environ.get("MEALCIRCUIT_TARGET_ARCH") if sys.platform == "darwin" else None
use_upx = sys.platform == "win32"
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
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
        "keyring.backends.chainer",
        "keyring.backends.fail",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
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
