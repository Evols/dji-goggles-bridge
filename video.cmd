@echo off
setlocal
cd /d "%~dp0"
title DJI Goggles Bridge - Video window

echo DJI Goggles Bridge - Video window
echo.
echo Before starting:
echo  1. Power on the goggles and connect a USB-C DATA cable to this PC.
echo  2. Settings ^> About ^> OTG Wired Connection to Computer: ON.
echo     The headset should show OTG Wired Connection - Connected.
echo  3. Press the 5D button DOWN, towards you, to open the shortcut menu.
echo     Turn ON Share Liveview to Mobile Device via Wi-Fi.
echo     That sharing option also enables the USB liveview path.
echo  4. Link the aircraft or air unit and confirm live camera video in the goggles.
echo     Without an aircraft: Settings ^> Camera ^> Advanced Camera Settings
echo     ^> Camera View Recording: ON, to stream the goggles' screen instead.
echo.
echo Press Q or Escape, or close the video window, to stop.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0network_check.ps1"
if errorlevel 1 goto network_failed
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creating the local Python environment...
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto setup_failed
)
".venv\Scripts\python.exe" -c "import av, numpy, cv2" >nul 2>&1
if errorlevel 1 (
    echo Installing video dependencies. Internet is needed for this step...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto setup_failed
)
".venv\Scripts\python.exe" viewer.py %*
if errorlevel 1 goto run_failed
exit /b 0

:setup_failed
echo.
echo Setup failed. Install Python 3.10 or newer with its PATH option enabled,
echo then try again. See README.md for details.
pause
exit /b 1

:run_failed
echo.
echo Check the USB cable, OTG, Wi-Fi LiveView sharing and the RNDIS address.
echo See README.md for setup and troubleshooting.
pause
exit /b 1

:network_failed
echo.
echo Startup stopped because the goggles network adapter is not ready.
echo Follow the instructions above, then run this launcher again.
pause
exit /b 1
