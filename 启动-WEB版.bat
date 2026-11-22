@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PATH=%~dp0tools\cli_shims;%PATH%"
set "PYW=C:\Users\MI\AppData\Local\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0app_web.py"
