@echo off
setlocal
title MC Stack
cd /d "%~dp0"

rem 編譯產物不在倉庫裡：沒 build 過就先引導去 build.bat
if not exist "MC-WS.exe" (
  echo [X] 找不到 MC-WS.exe —— 這個倉庫不附編譯好的執行檔。
  echo     請先安裝 Visual Studio C++（Community 免費），然後雙擊 build.bat 編譯。
  echo     （編譯只要一次，之後直接用 start.bat 就好）
  pause
  exit /b 1
)

echo [1/2] starting MC-WS.exe ...
start "MC Bridge" "MC-WS.exe"
timeout /t 3 /nobreak >nul
echo [2/2] starting Discord bot (auto restart) ...
call run_bot.bat
