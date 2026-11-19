@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=py -3.12
%PY% -c "import sys" >nul 2>nul || set PY=python

echo [1/3] 检查依赖...
%PY% -m pip install -q pyinstaller -r requirements.txt

echo [2/3] 打包（约 1~3 分钟）...
%PY% -m PyInstaller --noconfirm --clean --onefile --console ^
  --name SyuanTask ^
  --add-data "%~dp0src\templates;templates" ^
  --add-data "%~dp0src\pyapp;pyapp" ^
  --add-data "%~dp0src\units.json;." ^
  --add-data "%~dp0assets;assets" ^
  --icon "%~dp0assets\logo.ico" ^
  --collect-all playwright ^
  --hidden-import pyapp.captcha ^
  --paths src ^
  --exclude-module tkinter ^
  --exclude-module matplotlib ^
  --exclude-module IPython ^
  --distpath dist --workpath build --specpath build ^
  src\app.py

echo [3/3] 输出结果...
if exist "dist\SyuanTask.exe" (
  echo 完成：dist\SyuanTask.exe
  echo 把它单独拷走就能用，配置会保存在 exe 同目录的 config.json
) else (
  echo 打包失败，请查看上面的报错
)
pause
