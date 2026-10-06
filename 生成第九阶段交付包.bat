@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo 本脚本保留为兼容入口，转调正式 V3 交付流程。
call "%~dp0生成V3商业交付包.bat"
