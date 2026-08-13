@echo off
rem Pins the interpreter. pywebview lives in .venv, so launching main.py with
rem whatever python PATH happens to resolve first silently drops the WebGL UI.
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo   WARNING: no .venv found in this folder.
    echo   Falling back to the python on PATH - the 3D WebGL interface will
    echo   most likely be missing. To get it back:
    echo.
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    set "PY=python"
)

"%PY%" "%~dp0main.py"

rem Not %ERRORLEVEL% - this must be read at run time, and there is no
rem delayed expansion here. Keeps the window up so the traceback is readable.
if errorlevel 1 pause
endlocal
