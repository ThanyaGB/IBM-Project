@echo off
rem run_all.bat - One-command launcher for the health-myth-bot stack
rem
rem Starts:
rem   1. Python (prefer the project .venv)
rem   2. Reads FLASK_PORT from .env (default 5000)
rem   3. Seeds the SQLite database (idempotent)
rem   4. Flask webhook server in a separate window
rem   5. Streamlit dashboard in a separate window
rem   6. Cloudflare tunnel so Twilio can reach the local Flask port
rem
rem Prerequisites:
rem   * Run from the health-myth-bot folder.
rem   * cloudflared.exe exists next to this file.
rem   * .env contains TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, GEMINI_API_KEY,
rem     OPENAI_API_KEY, or the same values are set as environment variables.
rem
rem Usage:
rem   run_all.bat
rem
rem Stopping:
rem   Close the Flask and Streamlit windows, or stop cloudflared with Ctrl+C.
rem   The tunnel URL is only valid while cloudflared is running.

setlocal

echo.
echo ========================================
echo   Health Myth-Bot -- Launcher
echo   Starts Flask + Streamlit + tunnel
echo ========================================
echo.

rem Step 1: locate Python
set "PYTHON="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python not found. Activate the venv or install Python 3.11+.
        exit /b 1
    )
    for /f "delims=" %%P in ('where python') do set "PYTHON=%%P"
)
echo [1/5] Python: %PYTHON%

rem Step 2: read FLASK_PORT from .env
set "ENVFILE=%~dp0.env"
set "FLASK_PORT=5000"
if exist "%ENVFILE%" (
    for /f "usebackq tokens=1,2 delims==" %%A in ("%ENVFILE%") do (
        if /i "%%A"=="FLASK_PORT" set "FLASK_PORT=%%B"
    )
)
echo [2/5] Flask port: %FLASK_PORT%

rem Step 3: seed the database
echo.
echo [3/5] Seeding database...
"%PYTHON%" seed_data.py
if errorlevel 1 (
    echo seed_data.py exited with code %errorlevel% - continuing anyway.
)

rem Step 4: start Flask in its own window
echo.
echo [4/5] Starting Flask webhook server (port %FLASK_PORT%)
start "Flask - health-myth-bot" cmd /k "cd /d "%~dp0" && "%PYTHON%" app.py"
echo       Flask window opened.

rem Step 5: start Streamlit in its own window
echo [5/5] Starting Streamlit dashboard (port 8501)
start "Streamlit - health-myth-bot" cmd /k "cd /d "%~dp0" && "%PYTHON%" -m streamlit run dashboard.py --server.port 8501 --server.headless true"
echo       Streamlit window opened.

rem Wait a moment so the servers can bind before the tunnel starts
timeout /t 5 /nobreak >nul

rem Step 6: Cloudflare tunnel
echo.
echo ========================================
echo   Starting Cloudflare Tunnel...
echo   watch for the trycloudflare.com URL below.
echo   copy it and append /webhook for Twilio.
echo ========================================
echo.

set "CFDIR=%~dp0"
set "CFEXE=%CFDIR%cloudflared.exe"
if not exist "%CFEXE%" (
    echo cloudflared.exe not found in project folder. Run from the health-myth-bot directory.
    exit /b 1
)

echo   running: %CFEXE% tunnel --url http://localhost:%FLASK_PORT%
echo.
echo +-----------------------------------------------------------------+
echo | YOUR WEBHOOK URL WILL APPEAR BELOW (HTTPS://...TRYCLOUDFLARE.COM) |
echo | APPEND  /WEBHOOK  AND PASTE IT INTO THE TWILIO CONSOLE.          |
echo |                                                                 |
echo | DASHBOARD: HTTP://LOCALHOST:8501                                 |
echo | FLASK API:  HTTP://LOCALHOST:%FLASK_PORT%/HEALTH                 |
echo +-----------------------------------------------------------------+
echo.

"%CFEXE%" tunnel --url "http://localhost:%FLASK_PORT%"
