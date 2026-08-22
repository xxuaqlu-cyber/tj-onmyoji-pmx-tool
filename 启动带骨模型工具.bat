@echo off
chcp 65001 >nul
title Onmyoji Rigged Mesh Tool
cd /d "%~dp0"

set "PY_EXE="
set "PY_ARGS="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY_EXE set "PY_EXE=%%P"
if not defined PY_EXE (
    for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY_EXE (
        set "PY_EXE=%%P"
        set "PY_ARGS=-3"
    )
)

if not defined PY_EXE (
    echo [ERROR] Python was not found.
    echo Please install 64-bit Python 3.10 or newer.
    echo.
    pause
    exit /b 1
)

echo Python: %PY_EXE%
echo Script: %~dp0onmyoji_rigged_mesh_gui.py
echo.
"%PY_EXE%" %PY_ARGS% -X utf8 "%~dp0onmyoji_rigged_mesh_gui.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo The GUI process has exited. Exit code: %EXIT_CODE%
if not "%EXIT_CODE%"=="0" (
    echo Copy the error text above and send it to me.
)
pause
exit /b %EXIT_CODE%
