@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=py -3.12"
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=python"

%PY% -c "import cv2, numpy, playwright, flask" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖，请稍候...
  %PY% -m pip install -q -r requirements.txt
)

echo 启动自动登录控制台...
%PY% src\app.py
pause
