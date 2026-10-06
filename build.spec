# -*- mode: python ; coding: utf-8 -*-
#
# 正式 Qt 发布构建：onedir（不自解压，启动更快）+ QtWebEngine。
#
# 说明：
#  - 默认主程序经 app_entry 启动 Qt；旧前端参数只会被拒绝，不会切换界面。
#  - 必须包含 PySide6、QtWebEngineProcess、Qt 资源、Web/K 线资源、图标及配置。
#  - build_qt.spec 仅保留一个兼容周期，不再作为正式构建入口。
#  - pandas/requests 在源码里已用 lazy_loader 惰性加载，故此处需把 lazy_loader 列为 hiddenimport。

import os
from pathlib import Path

import PySide6
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, is_module_or_submodule

akshare_datas = collect_data_files("akshare", includes=["file_fold/*.json"])
baostock_hiddenimports = collect_submodules(
    "baostock",
    filter=lambda name: not is_module_or_submodule(name, "baostock.demo"),
)
source_root = os.environ.get("STOCK_TOOL_PYI_SOURCE_ROOT", "").strip()
entry_script = os.path.join(source_root, "main.py") if source_root else "main.py"
pathex = [source_root] if source_root else []
stock_tool_root = os.path.join(source_root, "stock_tool") if source_root else "stock_tool"
mvp_config_datas = [(os.path.join(stock_tool_root, "config"), "stock_tool/config")]
ui_web_root = os.path.join(stock_tool_root, "ui", "web")
qt_datas = [(ui_web_root, "stock_tool/ui/web")]
icon_path = os.path.join(source_root, "assets", "stocklens.ico") if source_root else os.path.join("assets", "stocklens.ico")
pyside6_root = Path(PySide6.__file__).resolve().parent
qt_webengine_datas = collect_data_files(
    "PySide6",
    includes=["resources/*", "translations/qtwebengine_locales/*"],
)
qt_webengine_binaries = [(str(pyside6_root / "QtWebEngineProcess.exe"), "PySide6")]
stock_tool_hiddenimports = []
if os.path.isdir(stock_tool_root):
    for dirpath, _dirnames, filenames in os.walk(stock_tool_root):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, source_root or ".")
            module = os.path.splitext(rel)[0].replace(os.sep, ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            stock_tool_hiddenimports.append(module)

a = Analysis(
    [entry_script],
    pathex=pathex,
    binaries=qt_webengine_binaries,
    datas=akshare_datas + mvp_config_datas + qt_datas + qt_webengine_datas,
    hiddenimports=[
        "akshare",
        "pandas",
        "lazy_loader",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel",
        "PySide6.QtSvg",
    ]
    + baostock_hiddenimports
    + sorted(set(stock_tool_hiddenimports)),
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
    name="Stocklens",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=icon_path,
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
    name="Stocklens",
)
