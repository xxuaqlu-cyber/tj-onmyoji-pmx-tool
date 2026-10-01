@echo off
chcp 65001 >nul
setlocal
title Onmyoji Arena PMX Preview
set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "NEOX_GAME_PROFILE=moba"

set "PY_EXE="
set "PY_ARGS="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY_EXE (
    "%%P" -c "import numpy, PIL, pymeshio" >nul 2>&1
    if not errorlevel 1 set "PY_EXE=%%P"
)
if not defined PY_EXE (
    for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY_EXE (
        "%%P" -3 -c "import numpy, PIL, pymeshio" >nul 2>&1
        if not errorlevel 1 (
            set "PY_EXE=%%P"
            set "PY_ARGS=-3"
        )
    )
)
if not defined PY_EXE (
    echo [ERROR] Python with numpy, Pillow and pymeshio was not found.
    echo Install the project dependencies, then try again.
    pause
    exit /b 1
)

set "LOG_FILE=%ROOT%\pmx_preview_moba_error.log"
"%PY_EXE%" %PY_ARGS% -X utf8 "%ROOT%\pmx_preview_gui.py" "%ROOT%\rigged_models_moba\展示高模PMX" >"%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] PMX preview exited with code %EXIT_CODE%.
    echo Details were saved to: %LOG_FILE%
    type "%LOG_FILE%"
    pause
)
exit /b %EXIT_CODE%
