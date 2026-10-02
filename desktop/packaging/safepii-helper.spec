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

HERE = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(HERE, "desktop", "helper.py")],
    # desktop/ as well as the repo root: helper.py imports broker from its own
    # folder, after putting that folder on sys.path at runtime. PyInstaller reads
    # the import statement but resolves it against these paths, not against what
    # the program will do to sys.path later.
    pathex=[HERE, os.path.join(HERE, "desktop")],
    binaries=[],
    datas=[],
    # comtypes builds its typelib wrappers at import time, and uiautomation
    # reaches for them dynamically, so PyInstaller cannot see them by walking
    # imports. Naming them here is what keeps the frozen build from failing at
    # the first accessibility call rather than at startup.
    hiddenimports=["comtypes", "comtypes.stream", "comtypes.automation",
                   "comtypes.typeinfo", "comtypes.client", "comtypes.client._generate",
                   "uiautomation",
                   # The file broker. Named here as well as found on pathex,
                   # because helper.py imports it inside a try/except -- a build
                   # that quietly leaves it out produces a helper with no folder
                   # sharing and no stdio bridge, and says nothing about it.
                   "broker"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here needs the test suite or the REPL. `http.server` and `email`
    # were on this list and must not be: the sign-in loopback listener and the
    # broker are both built on http.server, which parses its headers with email.
    # Excluding a module the program imports does not shrink the build, it breaks
    # it at the first use -- here, at sign-in.
    excludes=["pytest", "pydoc", "doctest", "idlelib", "xmlrpc",
              "distutils", "setuptools"],
    noarchive=False,
)
# PyInstaller 6 dropped bytecode encryption, so there is no cipher argument and
# PYZ takes only the pure modules.
pyz = PYZ(a.pure)

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
