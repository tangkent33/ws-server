@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 舊的入口，保留給習慣 double click build.bat 的人。
rem 真正的建置流程已經改用 Gradle wrapper，實際上是呼叫 gradlew.bat build。
echo [i] build.bat 已改為呼叫 gradlew.bat build ...
call gradlew.bat build %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [X] 建置失敗，請看上面的訊息。
  pause
)
exit /b %RC%
