# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：目录模式（onedir），免安装文件夹交付。"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

block_cipher = None

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "mutagen",
        "mutagen.wave", "mutagen.aiff", "mutagen.mp3", "mutagen.flac",
        "mutagen.mp4", "mutagen.oggvorbis", "mutagen.oggopus", "mutagen.asf",
        "mutagen.apev2", "mutagen.wavpack", "mutagen.id3",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "pip", "setuptools", "py7zr", "rarfile"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="音乐整理工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "icons" / "app.ico") if (ROOT / "assets" / "icons" / "app.ico").exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="音乐整理工具",
)
