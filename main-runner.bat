@echo off
setlocal
REM Run the downloader from anywhere - a shortcut to this file works fine.
set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

pushd "%SCRIPT_DIR%"
"%PYTHON%" main.py %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

REM Keep the window open when double clicked, or whenever something failed.
if not "%EXIT_CODE%"=="0" goto :wait
if "%~1"=="" goto :wait
goto :end

:wait
echo.
pause

:end
endlocal
exit /b %EXIT_CODE%
