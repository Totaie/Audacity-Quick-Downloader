@echo off
setlocal
REM Run the downloader from anywhere - a shortcut to this file works fine.
set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "STAMP=%VENV_DIR%\.last-update"

if exist "%PYTHON%" goto :check_update

REM --- First run: build the virtual environment ---------------------------
set "BOOTSTRAP="
py -3 --version >nul 2>&1 && set "BOOTSTRAP=py -3"
if defined BOOTSTRAP goto :make_venv
python --version >nul 2>&1 && set "BOOTSTRAP=python"
if not defined BOOTSTRAP goto :no_python

:make_venv
echo [setup] Creating virtual environment in "%VENV_DIR%" ...
%BOOTSTRAP% -m venv "%VENV_DIR%"
if errorlevel 1 goto :venv_failed
if not exist "%PYTHON%" goto :venv_failed
set "FRESH=1"

REM --- Keep gamdl and yt-dlp current --------------------------------------
:check_update
if defined AQD_SKIP_UPDATE goto :run
if defined FRESH goto :update
if not exist "%STAMP%" goto :update
set "LAST="
set /p LAST=<"%STAMP%"
REM Only check once a day; set AQD_SKIP_UPDATE=1 to skip it entirely.
if "%LAST%"=="%DATE%" goto :run

:update
if defined FRESH echo [setup] Installing dependencies, this takes a minute ...
if not defined FRESH echo [setup] Checking gamdl / yt-dlp for updates ...
"%PYTHON%" -m pip install --upgrade --disable-pip-version-check -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 goto :update_failed
> "%STAMP%" echo %DATE%
echo.
goto :run

:update_failed
if defined FRESH goto :install_failed
echo [setup] Update check failed - carrying on with what is installed.
echo.
goto :run

REM --- Run it -------------------------------------------------------------
:run
pushd "%SCRIPT_DIR%"
"%PYTHON%" main.py %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

REM Keep the window open when double clicked, or whenever something failed.
if not "%EXIT_CODE%"=="0" goto :wait
if "%~1"=="" goto :wait
goto :end

:no_python
echo [setup] No Python found on your PATH.
echo         Install Python 3 from https://www.python.org/downloads/ and
echo         tick "Add python.exe to PATH" during setup.
set "EXIT_CODE=1"
goto :wait

:venv_failed
echo [setup] Could not create the virtual environment in "%VENV_DIR%".
echo         Delete that folder if it exists and try again.
set "EXIT_CODE=1"
goto :wait

:install_failed
echo [setup] Installing the dependencies failed. Check your internet
echo         connection and run this file again.
set "EXIT_CODE=1"
goto :wait

:wait
echo.
pause

:end
endlocal
exit /b %EXIT_CODE%
