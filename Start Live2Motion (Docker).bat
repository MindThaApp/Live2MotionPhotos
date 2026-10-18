@echo off
setlocal EnableDelayedExpansion
REM Double-click this file (Windows) to start Live2Motion Photos in Docker. No
REM editing of docker-compose.yml is needed - only the photos folder you choose is
REM mounted, and everything (including which folder to convert) is
REM configured from the web UI.
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker is required but wasn't found.
  echo Install Docker Desktop from https://www.docker.com/products/docker-desktop/, then run this again.
  pause
  exit /b 1
)

if not exist config.json (
  copy config.example.json config.json >nul
)

if not exist .env (
  REM Mount only the photos folder (not the whole user folder).
  set "MEDIA=%USERPROFILE%\Pictures"
  set /p "MEDIA=Photos folder to give Live2Motion access to [%USERPROFILE%\Pictures]: "
  if not exist "!MEDIA!\" (
    echo Folder not found: !MEDIA!
    pause
    exit /b 1
  )
  echo HOST_MEDIA=!MEDIA!> .env
)

docker compose up -d --build

start "" cmd /c "timeout /t 3 >nul && start http://localhost:7000"

echo.
echo Live2Motion is running in the background ^(Docker^).
echo Your photos folder is mounted at /data - pick the exact photo folder under it from the in-app folder browser.
echo To use a different photos folder, edit HOST_MEDIA in .env ^(not docker-compose.yml^).
echo To stop it later: docker compose down
pause
