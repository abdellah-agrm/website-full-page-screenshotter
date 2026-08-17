# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Automatically collect CustomTkinter themes and assets
datas = collect_data_files('customtkinter')

# Add project icon
if os.path.exists('website_screenshotter_icon.png'):
    datas.append(('website_screenshotter_icon.png', '.'))

hiddenimports = collect_submodules('customtkinter') + [
    'PIL',
    'PIL.ImageTk',
    'playwright',
    'playwright.async_api',
    'requests',
    'bs4',
    'asyncio',
    'urllib.request',
    'urllib.parse'
]

a = Analysis(
    ['website_screenshotter.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ScreenshotterPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='website_screenshotter_icon.png' if os.path.exists('website_screenshotter_icon.png') else None,
)
