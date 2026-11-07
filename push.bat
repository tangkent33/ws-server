@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MC Bridge - git push

echo ============================================
echo   Git push  (把本機改動上傳到 GitHub)
echo ============================================
echo.

where git >nul 2>nul
if errorlevel 1 goto nogit

if not exist ".git" goto init

git remote get-url origin >nul 2>nul
if errorlevel 1 goto setremote
goto stage

:init
echo [*] 這個資料夾還不是 git 倉庫，先初始化...
git init
if errorlevel 1 goto fail
git branch -M main
goto setremote

:setremote
echo.
echo [*] 還沒設定遠端 origin
set "REPO_URL=%~1"
if not "%REPO_URL%"=="" goto addremote
set /p REPO_URL=請貼上 GitHub 倉庫網址:
if "%REPO_URL%"=="" goto noremote

:addremote
git remote add origin "%REPO_URL%"
if errorlevel 1 goto fail
goto stage

:noremote
echo [X] 沒有輸入網址，結束。
echo     也可以直接用參數: push.bat https://github.com/帳號/ws-server.git
goto end

:stage
rem 第一次提交需要身分，沒設定就補一個本機預設值(只影響這個倉庫)
git config user.email >nul 2>nul
if errorlevel 1 git config user.email "tangkent3@users.noreply.github.com"
git config user.name >nul 2>nul
if errorlevel 1 git config user.name "tangkent3"

rem 安全檢查: .env 裡有 Discord Token，絕對不能推上去
git ls-files --error-unmatch .env >nul 2>nul
if not errorlevel 1 goto envtracked

echo [*] 收集變更...
git add -A
if errorlevel 1 goto fail

git diff --cached --quiet
if not errorlevel 1 goto nothing

echo.
echo --- 這次要上傳的變更 ---
git status --short
echo.

set "MSG=%~2"
if not "%MSG%"=="" goto commit
set /p MSG=提交訊息(Enter 用預設，中文在 cmd 裡可能變亂碼):
if "%MSG%"=="" set "MSG=update"

:commit
git commit -m "%MSG%"
if errorlevel 1 goto fail

echo.
echo [*] 推送到 origin...
git push -u origin HEAD
if errorlevel 1 goto pushfail

echo.
echo [OK] 已上傳到 GitHub
goto end

:nothing
echo [i] 沒有新變更，不用提交。
goto end

:envtracked
echo [X] 危險: .env 已經被 git 追蹤了，裡面有 Discord Token！
echo     先執行以下兩行把它從追蹤中移除，再重跑這個腳本:
echo       git rm --cached .env
echo       git commit -m "stop tracking .env"
goto end

:pushfail
echo.
echo [!] push 失敗。如果是認證問題:
echo     - HTTPS: 需要 Personal Access Token 當密碼(GitHub 已不接受帳號密碼)
echo     - 或改用 SSH: git remote set-url origin git@github.com:帳號/ws-server.git
echo     如果是遠端有新提交: 先跑 pull.bat
goto end

:nogit
echo [X] 找不到 git。請先安裝 Git for Windows: https://git-scm.com/download/win
goto end

:fail
echo [X] 上一步失敗，請看上面的錯誤訊息。
goto end

:end
echo.
pause
