@echo off
setlocal EnableDelayedExpansion

set "DIR=%~dp0"
set "ENV_FILE=%DIR%.env"

if not exist "%ENV_FILE%" (
    echo Error: Missing %ENV_FILE%. Copy .env.example to .env and configure it. 1>&2
    exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    set "LINE=%%A"
    if not "!LINE!"=="" if not "!LINE:~0,1!"=="#" (
        set "%%A=%%B"
    )
)

if "%HOST%"=="" (
    echo Error: Required key HOST is missing or empty in .env 1>&2
    exit /b 1
)
if "%PORT%"=="" (
    echo Error: Required key PORT is missing or empty in .env 1>&2
    exit /b 1
)

set "URL=http://%HOST%:%PORT%"
echo Starting Offline Stream Player on %URL%...

cd /d "%DIR%"
if /i "%AUTO_OPEN_BROWSER%"=="true" (
    if "%BROWSER%"=="" (
        start "" "%URL%"
    ) else (
        start "" "%BROWSER%" "%URL%"
    )
)

python "%DIR%app.py"
