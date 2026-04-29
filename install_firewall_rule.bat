@echo off
REM Run as Administrator (right-click → Run as administrator)
REM Adds a Windows Firewall rule to allow inbound UDP to python.exe.
REM This makes the EmotiBit raw-UDP scan succeed in ~3 seconds instead
REM of falling back to BrainFlow's slower native discovery.

echo ===================================================================
echo  BiHome - Windows Firewall rule for Python (EmotiBit UDP)
echo ===================================================================
echo.

REM Find the active python.exe (assumes Anaconda layout; adjust if needed)
set PY="%USERPROFILE%\anaconda3\python.exe"
if not exist %PY% (
    echo Searching for python.exe...
    for /f "delims=" %%i in ('where python 2^>nul') do (
        set PY="%%i"
        goto :found
    )
)
:found
echo Using: %PY%
echo.

netsh advfirewall firewall delete rule name="BiHome Python (UDP inbound)" >nul 2>&1
netsh advfirewall firewall add rule ^
    name="BiHome Python (UDP inbound)" ^
    dir=in ^
    action=allow ^
    program=%PY% ^
    protocol=UDP ^
    profile=any
if errorlevel 1 (
    echo.
    echo [!] Failed to add firewall rule. Did you run this as Administrator?
    pause
    exit /b 1
)
echo.
echo [OK] Firewall rule added. Future BiHome runs will detect EmotiBits via
echo      raw UDP in seconds instead of BrainFlow fallback.
echo.
pause
