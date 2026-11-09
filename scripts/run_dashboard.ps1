<#
.SYNOPSIS
    Launcher script for AmazonAffiliateBot Management Dashboard in PowerShell.
.DESCRIPTION
    Starts the local dashboard, waits for HTTP readiness, and opens it only
    after the endpoint responds. Existing instances are reused.
#>

[CmdletBinding()]
param(
    [int]$Port = 5000,
    [switch]$OpenBrowser,
    [switch]$RestartExisting
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

$DashboardUrl = "http://127.0.0.1:$Port/"
$HealthUrl = "http://127.0.0.1:$Port/healthz"
$DashboardScript = Join-Path $ProjectRoot "amazon_affiliate.py"

function Stop-ExistingDashboard {
    # 1. Terminate process listening on the dashboard port if it's Python or our dashboard
    $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener -and $listener.OwningProcess) {
        $p = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and ($p.ProcessName -like "*python*" -or (Test-DashboardReady))) {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }

    # 2. Also terminate any matching dashboard processes by command line
    $projectMarker = $ProjectRoot.ToLowerInvariant()
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $commandLine = ([string]$_.CommandLine).ToLowerInvariant()
        $isDashboard = $commandLine -match '(^|\s)(amazon_affiliate\.py|dashboard\.py)(\s|$)'
        $commandLine -and $isDashboard -and ($commandLine.Contains("--dashboard") -or $commandLine.Contains("dashboard.py")) -and ($commandLine.Contains($projectMarker) -or $commandLine.Contains("amazonaffiliatebot"))
    }
    foreach ($existing in $processes) {
        Stop-Process -Id $existing.ProcessId -Force -ErrorAction SilentlyContinue
    }

    # 3. Wait up to 3 seconds for port to clear
    for ($i = 0; $i -lt 6; $i++) {
        $stillListening = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $stillListening) { break }
        Start-Sleep -Milliseconds 500
    }
}

function Test-DashboardReady {
    $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) {
        return $false
    }

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

if ($RestartExisting) {
    Stop-ExistingDashboard
}

if (-not $RestartExisting -and (Test-DashboardReady)) {
    Write-Host "[OK] Dashboard already running at $DashboardUrl" -ForegroundColor Green
    if ($OpenBrowser) { Start-Process $DashboardUrl }
    exit 0
}

if (Test-DashboardReady) {
    Write-Host "[OK] Dashboard ready at $DashboardUrl" -ForegroundColor Green
    if ($OpenBrowser) { Start-Process $DashboardUrl }
    exit 0
}

$portOwner = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portOwner) {
    Write-Error "Port $Port is already occupied by another application (PID: $($portOwner.OwningProcess)), but it is not the AmazonAffiliateBot dashboard. No browser was opened."
    exit 1
}

Write-Host "[INFO] Starting dashboard on $DashboardUrl" -ForegroundColor Cyan
$logDir = Join-Path $ProjectRoot "data\logs"
$stdoutLog = Join-Path $logDir "dashboard.stdout.log"
$stderrLog = Join-Path $logDir "dashboard.stderr.log"
$process = Start-Process -FilePath $PythonExe -ArgumentList @($DashboardScript, "--dashboard", "--port", "$Port") -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog

$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) { break }
    if (Test-DashboardReady) { $ready = $true; break }
}

if (-not $ready) {
    $detail = "Dashboard did not become ready within 15 seconds."
    if (Test-Path $stderrLog) {
        $tail = Get-Content -LiteralPath $stderrLog -Tail 8 -ErrorAction SilentlyContinue
        if ($tail) { $detail += "`n" + ($tail -join "`n") }
    }
    Write-Error $detail
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    exit 1
}

Write-Host "[OK] Dashboard ready at $DashboardUrl" -ForegroundColor Green
if ($OpenBrowser) { Start-Process $DashboardUrl }
exit 0
