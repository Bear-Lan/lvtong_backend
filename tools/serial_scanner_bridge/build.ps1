param([string]$OutputDirectory = (Join-Path $PSScriptRoot 'dist'))
$ErrorActionPreference = 'Stop'
$scannerCompiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $scannerCompiler)) { $scannerCompiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework/v4.0.30319/csc.exe' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
& $scannerCompiler /nologo /target:winexe /platform:anycpu /optimize+ /utf8output "/out:$OutputDirectory/ScannerBridge.exe" /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.ServiceProcess.dll /reference:System.Management.dll /reference:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'ScannerBridge.cs')
if ($LASTEXITCODE -ne 0) { throw '扫码服务编译失败' }
$scannerActions = [ordered]@{
    '1-安装并开机启动.cmd' = '--install'
    '2-启动服务.cmd' = '--start'
    '3-停止服务.cmd' = '--stop'
    '4-重启服务.cmd' = '--restart'
    '5-卸载服务.cmd' = '--uninstall'
    '6-服务管理与地址配置.cmd' = ''
}
foreach ($scannerName in $scannerActions.Keys) {
    $scannerCommand = '@echo off' + "`r`n" + 'start "" "%~dp0ScannerBridge.exe" ' + $scannerActions[$scannerName] + "`r`n"
    [IO.File]::WriteAllText((Join-Path $OutputDirectory $scannerName), $scannerCommand, [Text.Encoding]::ASCII)
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $OutputDirectory '使用说明.md') -Force
Write-Output "Built $OutputDirectory/ScannerBridge.exe"
