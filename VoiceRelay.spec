# -*- mode: python ; coding: utf-8 -*-
# macOS / Windows 共用同一份打包定义（CI 也走这个文件，避免两边参数漂移）
import sys
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# miniaudio 是「单文件模块」不是 package，collect_all 对它什么都不收
# （构建日志会打 WARNING: ... not a package）。它真正干活的是 cffi 生成的
# 扩展 _miniaudio，而 _miniaudio 在 C 层 import _cffi_backend —— 这个依赖
# PyInstaller 的依赖分析看不见，必须显式声明，否则打包版一 import
# miniaudio 就 ModuleNotFoundError，wav 直接生成不出来。
hiddenimports += ['miniaudio', '_miniaudio', '_cffi_backend']


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='VoiceRelay',
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
    name='VoiceRelay',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='VoiceRelay.app',
        icon=None,
        bundle_identifier=None,
    )
