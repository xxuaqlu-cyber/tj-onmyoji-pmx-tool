@echo off
chcp 65001 >nul
setlocal
title Onmyoji Arena Resource Pull Tool
cd /d "%~dp0"
set "NEOX_GAME_PROFILE=moba"

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
    pause
    exit /b 1
)

"%PY_EXE%" %PY_ARGS% -X utf8 "%~dp0onmyoji_resource_pull_gui.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
