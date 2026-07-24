# Remove Jarvis from this machine (per-user).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1             # keep audit log & grants
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -PurgeData  # remove those too

param(
    [switch]$PurgeData   # also delete %APPDATA%\Jarvis (grants, audit log, tokens)
)

$ErrorActionPreference = "SilentlyContinue"
$appDir = Join-Path $env:LOCALAPPDATA "Jarvis\app"
$dataDir = Join-Path $env:APPDATA "Jarvis"

Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "Jarvis"
Get-Process pythonw, python | Where-Object { $_.Path -like "*\Jarvis\app\*" } | Stop-Process -Force

Remove-Item -Recurse -Force $appDir

if ($PurgeData) {
    Remove-Item -Recurse -Force $dataDir
    if (Test-Path $dataDir) {
        Write-Host "Warning: could not fully remove $dataDir." -ForegroundColor Yellow
    } else {
        Write-Host "User data (grants, audit log, tokens) removed."
    }
} else {
    Write-Host "Kept user data at $dataDir (grants + audit history)."
    Write-Host "Remove it later with:  Remove-Item -Recurse '$dataDir'"
}

if (Test-Path $appDir) {
    Write-Host "Warning: some app files could not be removed from $appDir (still in use?)." -ForegroundColor Yellow
    Write-Host "Close running Jarvis processes and re-run this script."
} else {
    Write-Host "Jarvis removed for user $env:USERNAME."
}
