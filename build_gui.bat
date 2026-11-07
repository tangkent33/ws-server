@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build MC-Bridge-GUI

where py >nul 2>nul
if not errorlevel 1 (set "PY=py -3") else (set "PY=python")

echo [1/3] 檢查依賴 ...
%PY% -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo     安裝 pywebview ...
  %PY% -m pip install pywebview
)
%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo     安裝 pyinstaller ...
  %PY% -m pip install pyinstaller
)

echo [2/3] 打包 exe（要 1~2 分鐘）...
%PY% -m PyInstaller --onefile --noconsole --name MC-Bridge-GUI ^
  --collect-submodules webview --collect-submodules clr_loader gui_app.py

if errorlevel 1 (
  echo [X] 打包失敗，看上面的訊息。
  pause
  exit /b 1
)

echo [3/3] 完成：dist\MC-Bridge-GUI.exe
pause
