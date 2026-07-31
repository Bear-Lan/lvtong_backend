# 下载 MediaMTX Windows amd64（不覆盖 mediamtx.local.yml）
# 用法: .\deploy\mediamtx\download.ps1

$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$Version = "v1.19.3"
$Url = "https://github.com/bluenviron/mediamtx/releases/download/$Version/mediamtx_${Version}_windows_amd64.zip"

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$Zip = Join-Path $Dir "mediamtx.zip"
$Tmp = Join-Path $Dir "_extract"

Write-Host "下载: $Url"
curl.exe -L --retry 5 --retry-delay 2 -o $Zip $Url
if (!(Test-Path $Zip) -or (Get-Item $Zip).Length -lt 1MB) {
  throw "下载失败，请手动下载 Windows amd64 包，解出 mediamtx.exe 放到 deploy\mediamtx\"
}

if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
Expand-Archive -Path $Zip -DestinationPath $Tmp -Force
Remove-Item $Zip -Force

$Exe = Get-ChildItem $Tmp -Filter "mediamtx.exe" -Recurse | Select-Object -First 1
if (!$Exe) { throw "压缩包内未找到 mediamtx.exe" }
Copy-Item $Exe.FullName (Join-Path $Dir "mediamtx.exe") -Force
Remove-Item $Tmp -Recurse -Force

Write-Host "OK: $(Join-Path $Dir 'mediamtx.exe')"
