@echo off
REM Double-click this file (Windows) to set up (first run only) and start
REM Live2Motion Photos, then open it in your browser. No terminal commands required.
cd /d "%~dp0"

REM ---- 1. Find a working Python 3 (the Microsoft Store alias stub does not count) ----
set "PYTHON="
call :trypython py -3
if not defined PYTHON call :trypython python
if not defined PYTHON call :findpython
if defined PYTHON goto havepython

echo.
echo Python 3 was not found on this computer.
echo ^(If Windows mentioned the Microsoft Store, that is just a placeholder - it is not a real Python.
echo  You can turn it off in Settings ^> Apps ^> Advanced app settings ^> App execution aliases.^)
echo.
where winget >nul 2>nul
if errorlevel 1 goto manualpython
choice /c YN /m "Install Python 3.12 now via winget"
if errorlevel 2 goto manualpython
winget install -e --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
call :findpython
if not defined PYTHON call :trypython py -3
if defined PYTHON goto havepython
echo.
echo Python was installed but could not be located. Close this window and run the launcher again.
pause
exit /b 1

:manualpython
echo Install Python 3 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
pause
exit /b 1

:havepython
echo Using Python: %PYTHON%

REM ---- 2. First-run setup ----
if exist venv\Scripts\python.exe goto venvok
if exist venv rmdir /s /q venv
echo First run - setting up ^(this takes a minute^)...
%PYTHON% -m venv venv
if errorlevel 1 goto setupfailed
if not exist venv\Scripts\python.exe goto setupfailed
venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto setupfailed
echo. > venv\.setup_done
goto venvok

:setupfailed
echo.
echo Setup failed ^(see the messages above^). Removing the incomplete environment so the next run starts clean.
if exist venv rmdir /s /q venv
pause
exit /b 1

:venvok
if exist venv\.setup_done goto setupdone
REM Environment exists from an older launcher version or an interrupted run: make sure deps are present.
venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto setupfailed
echo. > venv\.setup_done
goto setupdone

:setupdone
if not exist config.json copy config.example.json config.json >nul

REM ---- 3. ExifTool ----
call :findexiftool
for %%d in ("%LOCALAPPDATA%\Programs\ExifTool" "%ProgramFiles%\ExifTool" "%ProgramFiles(x86)%\ExifTool" "%LOCALAPPDATA%\Microsoft\WinGet\Links") do call :addexif "%%~d"
for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\OliverBetz.ExifTool*") do call :addexif "%%~d"
for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\OliverBetz.ExifTool*") do for /d %%s in ("%%~d\*") do call :addexif "%%~s"
exit /b 0

:addexif
REM Avoid parenthesised blocks here: PATH may contain "Program Files (x86)".
if not exist "%~1\exiftool.exe" exit /b 0
set "PATH=%~1;%PATH%"
exit /b 0
