@echo off
setlocal
where python.exe >nul 2>nul
if errorlevel 1 (
    py -3 -B "%~dp0app.py"
) else (
    python -B "%~dp0app.py"
)
if errorlevel 1 pause
