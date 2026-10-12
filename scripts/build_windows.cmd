@echo off
setlocal
where py >nul 2>&1
if errorlevel 1 (
    python "%~dp0build.py" %*
) else (
    py -3 "%~dp0build.py" %*
)
if errorlevel 1 (
    echo.
    echo Build failed. Install Python 3.10 or newer with PATH enabled and check the error above.
    pause
    exit /b 1
)
exit /b 0
