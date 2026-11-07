@echo off
setlocal
cd /d "%~dp0"

set "VCVARS="
for %%P in (
  "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
  "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
  "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
) do if exist %%P set "VCVARS=%%~P"

if not defined VCVARS (
  echo [X] vcvars64.bat not found. Install Visual Studio C++ tools.
  pause
  exit /b 1
)

rem old DLL may be locked by the running game: rename it first
if exist "827dll.dll" (
  del /q "827dll.dll" 2>nul
  if exist "827dll.dll" ren "827dll.dll" "827dll.old.dll" >nul 2>nul
)
del /q "827dll.old.dll" 2>nul

call "%VCVARS%" >nul

echo [1/2] compiling 827dll.dll ...
cl /nologo /LD /MT /O2 /utf-8 /EHsc ^
  "src\dll\dllmain.cpp" "src\dll\http.cpp" "src\dll\analysis.cpp" ^
  "src\dll\scan.cpp" "src\dll\inject.cpp" ^
  /Fe:"827dll.dll" /Fo:"src\dll\\" /link /INCREMENTAL:NO winhttp.lib
if errorlevel 1 goto fail

echo [2/2] compiling MC-WS.exe ...
del /q "MC-WS.exe" 2>nul
cl /nologo /MT /O2 /utf-8 /EHsc ^
  "src\exe\main.cpp" "src\exe\globals.cpp" "src\exe\ws.cpp" ^
  "src\exe\httpserver.cpp" "src\exe\events.cpp" "src\exe\winutil.cpp" ^
  /Fe:"MC-WS.exe" /Fo:"src\exe\\" /link /INCREMENTAL:NO ws2_32.lib advapi32.lib
if errorlevel 1 goto fail

del /q "src\dll\*.obj" 2>nul
del /q "src\exe\*.obj" 2>nul
echo.
echo [OK] build finished
pause
exit /b 0

:fail
echo.
echo [X] build failed
pause
exit /b 1

