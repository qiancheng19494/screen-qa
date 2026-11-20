@echo off
chcp 65001 >nul
cd /d "%~dp0"
node "C:/Users/MI/.dsh/profiles/desktop/node_modules/@liustack/modlens/dist/main.js" -i "%~dp0_zh_test3.png" -o "%~dp0_vis_out.json" --timeout 240000 > "%~dp0_vis.log" 2>&1
echo EXIT=%ERRORLEVEL% >> "%~dp0_vis.log"
