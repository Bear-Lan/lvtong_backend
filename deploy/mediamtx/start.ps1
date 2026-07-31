# 启动本仓库 MediaMTX（Windows）
# 用法: .\deploy\mediamtx\start.ps1
# 一般无需手动跑：python main.py 会自动拉起

$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$Bin = Join-Path $Dir "mediamtx.exe"
$Cfg = Join-Path $Dir "mediamtx.local.yml"

if (!(Test-Path $Bin)) {
  Write-Host "未找到 mediamtx.exe，正在下载..."
  & (Join-Path $Dir "download.ps1")
}

if (!(Test-Path $Bin)) {
  throw "mediamtx.exe 仍不存在，请检查网络后运行 deploy\mediamtx\download.ps1"
}
if (!(Test-Path $Cfg)) {
  throw "缺少配置: $Cfg"
}

Write-Host "启动 MediaMTX..."
Write-Host "  WHEP: http://127.0.0.1:8889/cam1/whep"
Write-Host "  API:  http://127.0.0.1:9997/v3/paths/list"
Write-Host "配置: $Cfg"
Write-Host "按 Ctrl+C 停止"
Set-Location $Dir
& $Bin $Cfg
