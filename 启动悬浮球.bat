@echo off
rem QuotaBall launcher - runs detached, no console window
start "" "%~dp0.venv-ball\Scripts\pythonw.exe" "%~dp0quotaball\ui\app.py"
