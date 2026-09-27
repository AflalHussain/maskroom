# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the SafePII desktop helper.

    pip install pyinstaller uiautomation
    pyinstaller desktop/packaging/safepii-helper.spec --noconfirm

One directory rather than one file. A one-file build unpacks itself into a
temporary folder on every launch, which costs a second or two of startup and,
more to the point, is the shape antivirus engines most often flag: a program
that installs a global keyboard hook and unpacks an executable at runtime is a
poor thing to hand a bank's endpoint team. The folder is wrapped in the
installer anyway, so the user never sees it.

`windowed` suppresses the console window. The helper's only interface is the
floating pill, and a console behind it looks like something has gone wrong.
"""
import os

block_cipher = None
HERE = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(HERE, "desktop", "helper.py")],
    pathex=[HERE],
    binaries=[],
    datas=[],
    # comtypes builds its typelib wrappers at import time, and uiautomation
    # reaches for them dynamically, so PyInstaller cannot see them by walking
    # imports. Naming them here is what keeps the frozen build from failing at
    # the first accessibility call rather than at startup.
    hiddenimports=["comtypes", "comtypes.stream", "comtypes.automation",
                   "comtypes.typeinfo", "comtypes.client", "comtypes.client._generate",
                   "uiautomation"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here needs the test suite, the REPL or a web server.
    excludes=["pytest", "unittest", "pydoc", "doctest", "idlelib",
              "email", "http.server", "xmlrpc", "distutils", "setuptools"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SafePIIHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # packing is another antivirus red flag, for no real gain
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, "safepii.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SafePIIHelper",
)
