@echo off
setlocal EnableExtensions

REM ============================================================
REM  A-share ETF tracker - one-click update
REM  fetch snapshot -> compute metrics -> write data.json -> inject index.html
REM
REM  Put this file in the project root (same folder as update.py).
REM  It always works from its own folder, so double-click and
REM  shortcuts from anywhere both work.
REM
REM  Extra arguments are passed through to update.py, e.g.
REM      update.bat --check
REM      update.bat --no-fetch
REM ============================================================

cd /d "%~dp0"

if not exist "update.py" (
    echo [ERROR] update.py not found in:
    echo         %CD%
    echo         Put update.bat in the project root, next to update.py.
    goto :fail
)

echo ============================================================
echo  Project : %CD%
echo  Started : %date% %time%
echo ============================================================
echo.

REM ---------- locate python ----------
REM  Priority: python on PATH > py launcher > project .venv > home fixed path
REM   Office machine: python is on PATH (D:\env\Python312), hit at step 1.
REM   Home machine:   python is NOT on PATH; falls back to E:\ fixed install.
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
)
if not defined PY (
    if exist "E:\workApp\python\python.exe" set "PY=E:\workApp\python\python.exe"
)

if not defined PY (
    echo [ERROR] Python not found.
    echo         Install Python 3.9+ and tick "Add Python to PATH".
    goto :fail
)

echo Python  : %PY%
echo.

REM ---------- check and install dependencies ----------
%PY% -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo [INFO] requests not found, installing ...
    %PY% -m pip install --quiet requests
    if errorlevel 1 (
        echo [ERROR] pip install failed. Run manually:
        echo         %PY% -m pip install requests
        goto :fail
    )
    echo [OK]    requests installed.
)
echo.

REM ---------- run ----------
%PY% update.py %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo ============================================================
    echo  [DONE] Data updated.
    echo ============================================================
    echo %* | findstr /i /c:"--check" >nul
    if errorlevel 1 (
        if exist "index.html" start "" "index.html"
    ) else (
        echo  Check-only run: no file written, index.html not opened.
    )
) else (
    echo ============================================================
    echo  [FAILED] exit code %RC%
    echo  Previous data was kept; index.html still opens fine.
    echo ============================================================
)

echo.
echo Closing in 5 seconds ...
ping -n 6 127.0.0.1 >nul 2>nul
exit /b %RC%

:fail
echo.
echo Closing in 10 seconds ...
ping -n 11 127.0.0.1 >nul 2>nul
exit /b 1
