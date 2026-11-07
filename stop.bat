@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (set "PY=py -3") else (set "PY=python")

echo [1/4] 请求 bot 正常下线（会先移除 Discord 上的斜线指令）...
%PY% bot.py --stop
if errorlevel 1 echo       （请求失败，改用强制结束）

echo [2/4] 停止守护循环 (run_bot) ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo [3/4] 清理残留的 bot 进程 ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'py.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo [4/4] 停止 MC-WS.exe ...
taskkill /im MC-WS.exe /f >nul 2>nul

echo.
echo [OK] 已停止。剩余进程：
tasklist /fi "imagename eq python.exe" | findstr /i python
tasklist /fi "imagename eq MC-WS.exe" | findstr /i MC-WS
pause
