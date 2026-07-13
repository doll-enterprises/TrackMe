@echo off
REM Run the TrackMe regression tests (parser + death-snapshot) using the Python
REM launcher (py), which avoids the Microsoft Store "python" alias stub. Runs
REM from this file's folder so it works whatever directory you launch it from.
cd /d "%~dp0"
py test_parse.py
echo.
if errorlevel 1 (
    echo TESTS FAILED. Press any key to close.
) else (
    echo Tests passed. Press any key to close.
)
pause >nul
