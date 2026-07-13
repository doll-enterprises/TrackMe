@echo off
REM Launch TrackMe using the Python launcher (py), which avoids the Microsoft
REM Store "python" alias stub. Runs from this file's folder so the path to
REM trackme.py is always correct, whatever directory you launch it from.
cd /d "%~dp0"
py trackme.py %*
if errorlevel 1 (
    echo.
    echo TrackMe exited with an error. Press any key to close.
    pause >nul
)
