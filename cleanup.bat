@echo off
rem Delete leftover files: stale injected DLL copies and logs.
rem Run this after Minecraft is fully closed.
cd /d "%~dp0"
del /q "827dll_*.dll" 2>nul
del /q "bridge.log" "bridge.err.log" 2>nul
del /q "bot.out.log" 2>nul
del /q /s "__pycache__" 2>nul
if exist "827dll_*.dll" (
  echo [!] some DLL copies are still loaded by Minecraft - close the game and run again.
) else (
  echo [OK] cleaned.
)
pause
