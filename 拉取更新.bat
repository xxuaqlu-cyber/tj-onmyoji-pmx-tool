@echo off
chcp 65001 >nul
setlocal
title Onmyoji Resource Pull Tool
cd /d "%~dp0"

set "PY_EXE="
set "PY_ARGS="
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY_EXE (
    set "PY_EXE=%%P"
    set "PY_ARGS=-3"
)
if not defined PY_EXE (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY_EXE set "PY_EXE=%%P"
)

if not defined PY_EXE (
    echo [ERROR] Python was not found.
    echo Install 64-bit Python 3.10 or newer and try again.
    pause
    exit /b 1
)

if not exist "%~dp0onmyoji_resource_pull_gui.py" (
    echo [ERROR] onmyoji_resource_pull_gui.py was not found beside this BAT file.
    pause
    exit /b 1
)

"%PY_EXE%" %PY_ARGS% -X utf8 "%~dp0onmyoji_resource_pull_gui.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Resource pull tool exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
