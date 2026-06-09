# -*- mode: python ; coding: utf-8 -*-
# Linux spec — produces a single-folder distribution.
# Build with:  pyinstaller "Horizon Network Editor-linux.spec"
#
# Keyring note: on Linux, keyring uses SecretService (GNOME Keyring / KWallet)
# via the secretstorage and jeepney packages.  Install them before building if
# you want passwords to persist across runs:
#   pip install secretstorage jeepney
# The app works without them but will prompt for a password every session.

a = Analysis(
    ['horizon_network_editor.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('logo.ico', '.'),
    ],
    hiddenimports=[
        'requests',
        'horizon_functions',
        'horizon_app',
        'keyring',
        'keyring.backends',
        'keyring.backends.SecretService',
        'keyrings.alt',
        'keyrings.alt.file',
        'secretstorage',
        'jeepney',
        'loguru',
        'PySide6.QtWidgets',
        'PySide6.QtCore',
        'PySide6.QtGui',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Horizon Network Editor',
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
    icon='logo.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Horizon Network Editor',
)
