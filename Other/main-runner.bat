@echo off
REM Get the directory of the batch file
set BAT_DIR=%~dp0

REM Change the directory to where your virtual environment is located (relative to the batch file)
cd /d "%BAT_DIR%.venv\Scripts"

REM Activate the virtual environment
call activate.bat

REM Change to the directory where your Python file is located (relative to the batch file)
cd /d "%BAT_DIR%"

REM Run the Python script
python main.py

REM Pause to keep the window open after execution
pause

REM Deactivate the virtual environment
deactivate