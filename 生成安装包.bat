@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Stocklens V3 安装包生成器
echo ========================================
echo Stocklens V3 / A股选股仪 V3 安装包生成器
echo ========================================
echo.
echo 正式商业流程：Nuitka standalone + Inno Setup 安装包
echo.

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY (
    echo [错误] 没有找到 Python，请先安装 Python 3.10 或以上版本。
    goto end
)

%PY% -m pip install -r requirements.txt
%PY% -m pip install -r requirements-build.txt
%PY% -m pip install -r requirements-v3.lock
%PY% tools\build_v3_nuitka.py
if errorlevel 1 (
    echo.
    echo [错误] V3 安装包生成失败，请查看上方输出。
    echo        前置条件：Nuitka 4.1.3、Inno Setup 6。
    goto end
)

echo.
echo [完成] 安装包已生成在 installer_output\Stocklens_V3_Setup.exe，内部 manifest 在 build\v3_build_manifest.json。

:end
echo.
pause
