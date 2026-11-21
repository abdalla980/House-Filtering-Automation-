# Registers the monitor as a Windows scheduled task: starts at logon,
# runs hidden in the background, restarts automatically if it crashes.
# Usage:   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
# Remove:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall
param([switch]$Uninstall)

$TaskName = "BerlinHousingMonitor"
$Dir = $PSScriptRoot

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed $TaskName."
    return
}

if (-not (Test-Path (Join-Path $Dir ".env"))) {
    throw ".env not found. Copy .env.example to .env and fill in TELEGRAM_TOKEN first."
}

# Use the same Python that has requests/bs4 installed, windowless variant
$Python = (& python -c "import sys; print(sys.executable)").Trim()
$PythonW = Join-Path (Split-Path $Python) "pythonw.exe"
if (-not (Test-Path $PythonW)) { throw "pythonw.exe not found next to $Python" }

$Action   = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Dir\monitor.py`"" -WorkingDirectory $Dir
$Trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Kleinanzeigen Berlin housing alerts to Telegram" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Installed and started $TaskName (using $PythonW). Log: $Dir\monitor.log"
