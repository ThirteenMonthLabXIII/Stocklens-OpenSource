# -*- mode: python ; coding: utf-8 -*-
#
# 兼容构建：保留一个周期，供已有自动化或历史候选包复现使用。
# 正式发布统一使用 build.spec；它包含 PySide6 + QtWebEngine 的 Qt-only 入口。
# 不需要前端选择环境变量。
#
# 打包：python -m PyInstaller build_qt.spec  ->  dist/A股选股仪_Qt版/

from pathlib import Path

import PySide6
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, is_module_or_submodule

akshare_datas = collect_data_files("akshare", includes=["file_fold/*.json"])
baostock_hiddenimports = collect_submodules(
    "baostock",
    filter=lambda name: not is_module_or_submodule(name, "baostock.demo"),
)
qt_datas = [("stock_tool/ui/web", "stock_tool/ui/web")]
mvp_config_datas = [("stock_tool/config", "stock_tool/config")]
pyside6_root = Path(PySide6.__file__).resolve().parent
qt_webengine_datas = collect_data_files(
    "PySide6",
    includes=[
        "resources/*",
        "translations/qtwebengine_locales/*",
    ],
)
qt_webengine_binaries = [(str(pyside6_root / "QtWebEngineProcess.exe"), "PySide6")]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=qt_webengine_binaries,
    datas=akshare_datas + qt_datas + mvp_config_datas + qt_webengine_datas,
    hiddenimports=[
        "akshare",
        "pandas",
        "lazy_loader",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel",
        "PySide6.QtSvg",
    ]
    + baostock_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt6", "PyQt5", "PySide2"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="A股选股仪_Qt版",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="A股选股仪_Qt版",
)
