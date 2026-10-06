@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在构建 Stocklens V3 Nuitka standalone，第一次会比较慢...
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python -m pip install -r requirements.txt
    python -m pip install -r requirements-build.txt
    python -m pip install -r requirements-v3.lock
    python tools\build_v3_nuitka.py
    goto done
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install -r requirements.txt
    py -3 -m pip install -r requirements-build.txt
    py -3 -m pip install -r requirements-v3.lock
    py -3 tools\build_v3_nuitka.py
    goto done
)

echo 没有找到 Python，请先安装 Python 3.10 或以上版本。
goto end

:done
echo.
echo 打包完成后，产物在 dist\Stocklens_V3\ 文件夹里（standalone 形态，启动无需自解压）。
echo 运行请双击该文件夹内的 Stocklens.exe；整个文件夹一起分发，不要把 exe 单独拖出。
echo 如需生成安装包，请改运行 生成安装包.bat（需已安装 Inno Setup）。

:end
echo.
pause
