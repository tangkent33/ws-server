@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MC Bridge - git pull

echo ============================================
echo   Git pull  (從 GitHub 拉最新版)
echo ============================================
echo.

where git >nul 2>nul
if errorlevel 1 goto nogit

if not exist ".git" goto init

git remote get-url origin >nul 2>nul
if errorlevel 1 goto setremote
goto pull

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
goto pull

:noremote
echo [X] 沒有輸入網址，結束。
echo     也可以直接用參數: pull.bat https://github.com/帳號/ws-server.git
goto end

:pull
echo.
set "BRANCH="
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%b"
if "%BRANCH%"=="" set "BRANCH=main"
if "%BRANCH%"=="HEAD" set "BRANCH=main"
echo [*] 分支 %BRANCH%，從 origin 拉取...
git pull --rebase --autostash origin %BRANCH%
if errorlevel 1 goto conflict
git branch --set-upstream-to=origin/%BRANCH% %BRANCH% >nul 2>nul

echo.
echo [OK] 已更新到最新版
goto end

:conflict
echo.
echo [!] pull 沒有成功，常見原因:
echo     1) 本機有未提交的改動        -^> 先跑 push.bat，或 git stash
echo     2) 兩邊歷史不相干(第一次拉)  -^> git pull origin main --allow-unrelated-histories
echo     3) 分支不是 main             -^> git branch -M main 後再試
echo     4) 遠端有同名但未追蹤的檔案  -^> 先把本機那份改名或刪掉(或用 git stash -u)
echo.
echo --- 目前狀態 ---
git status --short
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
