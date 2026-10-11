<#
.SYNOPSIS
    Launcher script for AmazonAffiliateBot Management Dashboard in PowerShell.
.DESCRIPTION
    Launches the local web dashboard at http://127.0.0.1:5000 in DRY_RUN mode.
#>

[CmdletBinding()]
param(
    [int]$Port = 5000
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path "$ScriptDir\..").Path
Set-Location $ProjectRoot

# Ensure data directories exist
@("data\logs", "data\queue", "data\products", "data\generated") | ForEach-Object {
    if (!(Test-Path $_)) { New-Item -ItemType Directory -Path $_ -Force | Out-Null }
}

# Resolve Python interpreter
$PythonExe = "python"
if (Test-Path ".venv\Scripts\python.exe") {
    $PythonExe = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
    $PythonExe = "venv\Scripts\python.exe"
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " Starting AmazonAffiliateBot Management Dashboard" -ForegroundColor Cyan
Write-Host " URL: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host " Mode: DRY RUN (No live posts published)" -ForegroundColor Yellow
Write-Host "=================================================================" -ForegroundColor Cyan

& $PythonExe amazon_affiliate.py --dashboard --port $Port
