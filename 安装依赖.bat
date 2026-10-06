@echo off
chcp 65001 >nul
cd /d "%~dp0"
title A股选股仪 依赖安装器
echo ========================================
echo A股选股仪 依赖安装器
echo ========================================
echo 当前目录：%cd%
echo.
echo 正在安装运行所需依赖，请保持网络连接...
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python --version
    echo.
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    goto end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 --version
    echo.
    py -3 -m pip install --upgrade pip
    py -3 -m pip install -r requirements.txt
    goto end
)

echo 没有找到 Python，请先安装 Python 3.10 或以上版本。

:end
echo.
echo 如果上面显示 Successfully installed 或 Requirement already satisfied，说明依赖已安装。
pause
