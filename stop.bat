@echo off
rem Stop the bridge and the Discord bot (including the hidden supervisor).
cd /d "%~dp0"
echo [1/3] stopping bot.py ...
wmic process where "name='python.exe' and commandline like '%%bot.py%%'" call terminate >nul 2>nul
wmic process where "name='py.exe' and commandline like '%%bot.py%%'" call terminate >nul 2>nul
echo [2/3] stopping supervisor ...
wmic process where "name='cmd.exe' and commandline like '%%run_bot%%'" call terminate >nul 2>nul
echo [3/3] stopping MC-WS.exe ...
taskkill /im MC-WS.exe /f >nul 2>nul
echo.
echo [OK] stopped. Remaining:
tasklist /fi "imagename eq python.exe" | findstr /i python
tasklist /fi "imagename eq MC-WS.exe" | findstr /i MC-WS
pause
