@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 让 modlens 能找到真 .exe 版本的 codex / claude（npm 的 .cmd 垫片 Node 起不来）
set "PATH=%~dp0tools\cli_shims;%PATH%"

set "PYW=C:\Users\MI\AppData\Local\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"

rem 走看门狗：启动失败会弹窗说明原因，而不是"点了没反应"
start "" "%PYW%" "%~dp0tools\run_gui.py"
endlocal
