param(
    [Parameter(Mandatory=$true)][string]$Path,
    [string]$OutFile = ""
)

# Windows 自带 OCR（Windows.Media.Ocr），通过 .NET 流读取图片，
# 避免 StorageFile 的 appmodel 权限问题。结果写入 UTF-8 文件（避免控制台编码问题）。

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

$fs = [System.IO.File]::OpenRead((Resolve-Path -LiteralPath $Path).Path)
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
$text = $lines -join "`n"

if ([string]::IsNullOrEmpty($OutFile)) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $text
} else {
    [System.IO.File]::WriteAllText($OutFile, $text, (New-Object System.Text.UTF8Encoding($false)))
}
