@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 让 modlens 能找到真 .exe 版本的 codex / claude（npm 的 .cmd 垫片 Node 起不来）
set "PATH=%~dp0tools\cli_shims;%PATH%"

set "PYW=C:\Users\MI\AppData\Local\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"

rem 直接起（1 秒级出窗口）；崩了由 app.py 自己弹窗报错并写 _startup_error.log
start "" "%PYW%" "%~dp0app.py"
endlocal
