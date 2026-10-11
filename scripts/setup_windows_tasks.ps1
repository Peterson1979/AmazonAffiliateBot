<#
.SYNOPSIS
    Interactive / Standalone Windows Task Scheduler Setup Script for AmazonAffiliateBot.
    DO NOT run automatically - inspect and execute with Administrator privileges when ready for go-live.

.DESCRIPTION
    Creates three scheduled tasks in Windows Task Scheduler:
    1. AmazonAffiliateBot_DailyMaintenance (Runs at 08:30 Europe/Budapest daily)
       -> Executes scripts\run_daily_maintenance.bat (--maintain-queue)
    2. AmazonAffiliateBot_PublishSlot1 (Runs at 12:00 Europe/Budapest daily)
       -> Executes scripts\run_daily_publisher.bat (--publish-queue)
    3. AmazonAffiliateBot_PublishSlot2 (Runs at 19:00 Europe/Budapest daily)
       -> Executes scripts\run_daily_publisher.bat (--publish-queue)

.NOTES
    Requires: Windows PowerShell 5.1+ / PowerShell 7+ run as Administrator.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path "$ScriptDir\..").Path

$MaintenanceBat = Join-Path $ProjectRoot "scripts\run_daily_maintenance.bat"
$PublisherBat = Join-Path $ProjectRoot "scripts\run_daily_publisher.bat"

$TaskMaintenanceName = "AmazonAffiliateBot_DailyMaintenance_0830"
$TaskSlot1Name       = "AmazonAffiliateBot_PublishSlot1_1200"
$TaskSlot2Name       = "AmazonAffiliateBot_PublishSlot2_1900"

if ($Uninstall) {
    Write-Host "[INFO] Removing AmazonAffiliateBot scheduled tasks..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskMaintenanceName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskSlot1Name -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskSlot2Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[SUCCESS] All AmazonAffiliateBot scheduled tasks unregistered." -ForegroundColor Green
    exit 0
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " AmazonAffiliateBot - Windows Task Scheduler Setup" -ForegroundColor Cyan
Write-Host " Project Root: $ProjectRoot" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

# Verify files exist
if (!(Test-Path $MaintenanceBat) -or !(Test-Path $PublisherBat)) {
    Write-Error "[ERROR] Launcher batch files not found in $ScriptDir."
    exit 1
}

# 1. Register Daily Maintenance Task (08:30)
$ActionMaint = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$MaintenanceBat`"" -WorkingDirectory $ProjectRoot
$TriggerMaint = New-ScheduledTaskTrigger -Daily -At "08:30"
$SettingsMaint = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskMaintenanceName -Action $ActionMaint -Trigger $TriggerMaint -Settings $SettingsMaint -Description "AmazonAffiliateBot Daily Discovery and Queue Replenishment at 08:30" -Force | Out-Null
Write-Host "[OK] Registered: $TaskMaintenanceName (Daily at 08:30)" -ForegroundColor Green

# 2. Register Slot 1 Publishing Task (12:00)
$ActionSlot1 = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$PublisherBat`"" -WorkingDirectory $ProjectRoot
$TriggerSlot1 = New-ScheduledTaskTrigger -Daily -At "12:00"
$SettingsSlot1 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskSlot1Name -Action $ActionSlot1 -Trigger $TriggerSlot1 -Settings $SettingsSlot1 -Description "AmazonAffiliateBot Daily Slot 1 Publishing at 12:00" -Force | Out-Null
Write-Host "[OK] Registered: $TaskSlot1Name (Daily at 12:00)" -ForegroundColor Green

# 3. Register Slot 2 Publishing Task (19:00)
$ActionSlot2 = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$PublisherBat`"" -WorkingDirectory $ProjectRoot
$TriggerSlot2 = New-ScheduledTaskTrigger -Daily -At "19:00"
$SettingsSlot2 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskSlot2Name -Action $ActionSlot2 -Trigger $TriggerSlot2 -Settings $SettingsSlot2 -Description "AmazonAffiliateBot Daily Slot 2 Publishing at 19:00" -Force | Out-Null
Write-Host "[OK] Registered: $TaskSlot2Name (Daily at 19:00)" -ForegroundColor Green

Write-Host "`n[SUCCESS] Windows Task Scheduler configuration is complete!" -ForegroundColor Cyan
