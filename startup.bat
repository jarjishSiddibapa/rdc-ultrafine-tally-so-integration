@echo off
rem ============================================================
rem  Ultrafine Sales Orders - start the complete application
rem  Double-click to start. Keep this window open while you use the app.
rem  Press Ctrl+C (or close the window) to stop it.
rem ============================================================
setlocal
cd /d "%~dp0"
title Ultrafine Sales Orders - running (Ctrl+C to stop)

set "PY=%~dp0venv\Scripts\python.exe"
set "URL=http://127.0.0.1:1401"

if not exist "%PY%" (
    echo [ERROR] Python environment not found: %PY%
    echo         Create it first:  python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] .env file is missing. Copy .env.example to .env and fill it in.
    pause
    exit /b 1
)

rem --- Already running in the background from an earlier start? Replace it. ----
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing '%URL%/healthz' -TimeoutSec 3).StatusCode | Out-Null } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
    echo An earlier instance is still running on port 1401 - stopping it first...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" >nul
    powershell -NoProfile -Command "Start-Sleep -Seconds 2"
)

rem --- 1. MySQL ---------------------------------------------------------------
echo [1/3] Checking MySQL service...
sc query MySQL95 | find "RUNNING" >nul
if errorlevel 1 (
    echo       MySQL95 is not running, starting it...
    net start MySQL95 >nul 2>&1
    sc query MySQL95 | find "RUNNING" >nul
    if errorlevel 1 (
        echo [ERROR] Could not start the MySQL95 service.
        echo         Right-click startup.bat and choose "Run as administrator", or start MySQL manually.
        pause
        exit /b 1
    )
)
echo       MySQL is running.

rem --- 2. Database migrations + first administrator ---------------------------
echo [2/3] Preparing the database...
"%PY%" -m alembic upgrade head
if errorlevel 1 (
    echo [ERROR] Database migration failed. Check the DB_* values in .env.
    pause
    exit /b 1
)
"%PY%" -m app.cli seed-admin
if errorlevel 1 (
    echo [ERROR] Could not seed the administrator. Check ADMIN_EMAIL and ADMIN_PASSWORD in .env.
    pause
    exit /b 1
)

rem --- 3. Server, in this window ----------------------------------------------
echo [3/3] Starting the server...
echo.
echo  Ultrafine Sales Orders:  %URL%
echo  Press Ctrl+C to stop.
echo.

rem Open the browser once the server answers (runs alongside the server).
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing '%URL%/healthz' -TimeoutSec 2 | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"

rem "<nul" stops cmd from asking "Terminate batch job (Y/N)?" after Ctrl+C.
"%PY%" -m app.cli serve <nul

echo.
echo The application has stopped.
timeout /t 3 >nul
exit /b 0
