@echo off
rem Windows 上一键打包 VoiceRelay.exe
rem 用法：在 Windows 上安装 Python 3.10+ 后，双击运行本文件
rem
rem 前置（首次运行会自动执行）：
python -m pip install --upgrade pip
python -m pip install requests playwright pyinstaller
python -m playwright install chromium
python -m playwright install-deps 2>nul

pyinstaller --noconfirm --onefile --console --name VoiceRelay --collect-all playwright app.py

echo.
echo ==========================================
echo 打包完成：dist\VoiceRelay.exe
echo 首次在别的电脑运行前，需安装浏览器内核：
echo   VoiceRelay.exe 启动登录时如报缺少浏览器，
echo   在该电脑执行: playwright install chromium
echo ==========================================
pause
