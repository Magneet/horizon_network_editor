# -*- mode: python ; coding: utf-8 -*-
# Mac spec — produces a self-contained .app bundle.
# Build with:  pyinstaller "Horizon Network Editor-mac.spec"

a = Analysis(
    ['horizon_network_editor.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('logo.ico', '.'),
        ('logo.icns', '.'),
    ],
    hiddenimports=[
        'requests',
        'horizon_functions',
        'horizon_app',
        'keyring',
        'keyring.backends',
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
    collect_all=['PySide6'],
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
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # None = native arch; set 'universal2' for fat binary
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.icns',
)

app = BUNDLE(
    exe,
    a.binaries,
    a.datas,
    name='Horizon Network Editor.app',
    icon='logo.icns',
    bundle_identifier='com.retouw.hneditor',
    info_plist={
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
    },
)
