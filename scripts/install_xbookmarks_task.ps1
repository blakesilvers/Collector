# Registers a daily Windows Task Scheduler job that runs `python -m xbookmarks` from this repo.
# Run from PowerShell inside the repo folder:  .\scripts\install_xbookmarks_task.ps1 -At 05:30
param(
    [string]$At = "05:30",
    [string]$TaskName = "X bookmarks collector",
    [string]$Python = ""
)
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { (Get-Command python).Source }
}
$action  = New-ScheduledTaskAction -Execute $Python -Argument "-m xbookmarks run" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Registered '$TaskName' to run daily at $At using $Python in $repo"
Write-Host "Set the Gmail App Password once (user scope) with:"
Write-Host '  [Environment]::SetEnvironmentVariable("XBOOKMARKS_SMTP_PASSWORD", "<app password>", "User")'
