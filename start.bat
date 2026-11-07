@echo off
chcp 65001 >nul
setlocal
title MC Stack
cd /d "%~dp0"

rem 編譯產物不在倉庫裡：沒 build 過就先引導去跑 gradlew
if not exist "MC-WS.exe" (
  echo [X] 找不到 MC-WS.exe —— 這個倉庫不附編譯好的執行檔，要先建置。
  echo.
  echo   1. 安裝 Visual Studio 2022 Community（免費），勾「使用 C++ 的桌面開發」
  echo   2. 安裝 JDK 17 或更新版本（Gradle wrapper 需要 Java）
  echo   3. 在專案目錄執行： gradlew.bat build
  echo.
  echo   編譯只要一次，之後直接用 start.bat 就好。
  pause
  exit /b 1
)

echo [1/2] starting MC-WS.exe ...
start "MC Bridge" "MC-WS.exe"
timeout /t 3 /nobreak >nul
echo [2/2] starting Discord bot (auto restart) ...
call run_bot.bat
