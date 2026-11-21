$ErrorActionPreference = 'Stop'
$desk = [Environment]::GetFolderPath('Desktop')
$dir  = Join-Path $desk '屏幕问答'
$bat  = Join-Path $dir '启动.bat'
$ico  = Join-Path $dir 'assets\app.ico'
$lnk  = Join-Path $desk '屏幕问答.lnk'
if (-not (Test-Path $bat)) { throw "找不到 $bat" }
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $bat
$sc.WorkingDirectory = $dir
$sc.IconLocation     = $ico
$sc.Description      = '屏幕问答 · DeepSeek 截图答题'
$sc.WindowStyle      = 7
$sc.Save()
$chk = $ws.CreateShortcut($lnk)
"lnk      : $lnk"
"Target   : $($chk.TargetPath)"
"Workdir  : $($chk.WorkingDirectory)"
"Icon     : $($chk.IconLocation)"
