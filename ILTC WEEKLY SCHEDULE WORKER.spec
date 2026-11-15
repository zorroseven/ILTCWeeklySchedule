# -*- mode: python ; coding: utf-8 -*-
# The automation worker (uiautomation). A separate build on purpose -
# pywebview and comtypes in one executable deadlock at startup. Installed
# as <app>\worker\ because two onedir builds can't share _internal.

a = Analysis(
    ['schedule_worker.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['site_core', 'infloww_core'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ILTC WEEKLY SCHEDULE WORKER',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ILTC WEEKLY SCHEDULE WORKER',
)
