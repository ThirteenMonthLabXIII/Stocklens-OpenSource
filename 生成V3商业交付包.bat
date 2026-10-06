@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo 正在构建 Stocklens V3 Nuitka 候选包...
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
python -m pip install -r requirements-v3.lock
python tools\build_v3_nuitka.py --candidate-stage rc0
if errorlevel 1 goto fail

echo 正在生成 V3 RC 交付目录并运行自动验收...
python tools\prepare_v3_release.py
if errorlevel 1 goto fail

echo [OK] V3 RC 候选已生成；请把 release\V3_商业交付_* 中的 P7 清单交给外部验收设备。
pause
exit /b 0

:fail
echo [FAIL] V3 候选生成失败，请查看上方输出。
pause
exit /b 1
