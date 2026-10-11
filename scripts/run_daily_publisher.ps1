# AmazonAffiliateBot - Daily Publishing Pipeline PowerShell Launcher
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path "$ScriptDir\.."
Set-Location $ProjectRoot

if (!(Test-Path "data\logs")) {
    New-Item -ItemType Directory -Force -Path "data\logs" | Out-Null
}

$PythonExe = "python"
if (Test-Path ".venv\Scripts\python.exe") {
    $PythonExe = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
    $PythonExe = "venv\Scripts\python.exe"
}

& $PythonExe amazon_affiliate.py --publish-queue $args
$ExitCode = $LASTEXITCODE
exit $ExitCode
