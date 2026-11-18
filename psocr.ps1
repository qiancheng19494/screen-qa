param(
    [string]$Path = "",
    [string]$OutFile = "",
    [string]$ListFile = "",
    [string[]]$PathList = @()
)

# Windows 自带 OCR（Windows.Media.Ocr），通过 .NET 流读取图片，
# 避免 StorageFile 的 appmodel 权限问题。结果写入 UTF-8 文件（避免控制台编码问题）。
# 单图：-Path 图 -OutFile out.txt  → 纯文本
# 批量：-PathList a.png,b.png -OutFile out.json → JSON {路径: 文本}
#       （批量只起一个进程，省掉每次 ~0.4 秒的 PowerShell/WinRT 启动开销）

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Error 'NO_OCR_ENGINE'; exit 3 }

function Ocr-One([string]$p) {
    $fs = [System.IO.File]::OpenRead((Resolve-Path -LiteralPath $p).Path)
    try {
        $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($fs)
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    } finally {
        $fs.Dispose()
    }
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $result.Lines) { $lines.Add($line.Text) }
    return ($lines -join "`n")
}

$utf8 = New-Object System.Text.UTF8Encoding($false)

# 批量模式的路径清单从 JSON 文件读（命令行直接传数组会被 -File 的参数绑定拆散）
if (-not [string]::IsNullOrEmpty($ListFile)) {
    $raw = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $ListFile).Path, $utf8)
    $PathList = @($raw | ConvertFrom-Json)
}

if ($PathList -and $PathList.Count -gt 0) {
    $map = [ordered]@{}
    foreach ($p in $PathList) {
        try { $map[$p] = (Ocr-One $p) } catch { $map[$p] = "" }
    }
    $json = $map | ConvertTo-Json -Compress
    if ([string]::IsNullOrEmpty($OutFile)) {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        Write-Output $json
    } else {
        [System.IO.File]::WriteAllText($OutFile, $json, $utf8)
    }
    exit 0
}

if ([string]::IsNullOrEmpty($Path)) { Write-Error 'NO_INPUT'; exit 4 }
$text = Ocr-One $Path

if ([string]::IsNullOrEmpty($OutFile)) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $text
} else {
    [System.IO.File]::WriteAllText($OutFile, $text, $utf8)
}
