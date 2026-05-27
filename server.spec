# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

trafilatura_datas = collect_data_files('trafilatura')

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('index.html', '.'),
        ('assets', 'assets'),
    ] + trafilatura_datas,
    hiddenimports=[
        'flask',
        'flask_cors',
        'requests',
        'bs4',
        'lxml',
        'tiktoken',
        'tiktoken_ext.openai_public',
        'tiktoken_ext',
        'charset_normalizer',
        'docx',
        'docx.oxml',
        'docx.oxml.ns',
        'reportlab',
        'reportlab.lib',
        'reportlab.platypus',
        'trafilatura',
        'trafilatura.settings',
        'trafilatura.core',
        'trafilatura.utils',
        'trafilatura.htmlprocessing',
        'trafilatura.main_extractor',
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
    name='server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,        # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='server',
)
