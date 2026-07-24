# Jarvis employee-machine installer (no admin required).
#
# Installs Jarvis into %LOCALAPPDATA%\Jarvis\app, runs the consent wizard,
# and registers autostart in the user session (HKCU Run key -> pythonw.exe).
#
# Usage (from a copy/clone of this repository):
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoAutostart

param(
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$appDir = Join-Path $env:LOCALAPPDATA "Jarvis\app"

function Assert-Exit($step) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$step failed (exit $LASTEXITCODE). Aborting install." -ForegroundColor Red
        exit 1
    }
}

Write-Host "== Installing Jarvis for $env:USERNAME ==" -ForegroundColor Cyan

# 0. Stop any running instance so files aren't locked
Get-Process pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*\Jarvis\app\*" } | Stop-Process -Force

# 1. Python 3.10+ ('py -3' covers launcher-only machines with newer Pythons)
$python = $null
foreach ($candidate in @("py -3.12", "py -3.11", "py -3.10", "py -3.13", "py -3", "python")) {
    try {
        $version = Invoke-Expression "$candidate --version" 2>$null
        if ($version -match "Python 3\.(1[0-9])") { $python = $candidate; break }
    } catch {}
}
if (-not $python) {
    Write-Host "Python 3.10+ not found. Ask IT to run: winget install Python.Python.3.12" -ForegroundColor Red
    exit 1
}

# 2. Copy the app and build its private venv
New-Item -ItemType Directory -Force $appDir | Out-Null
robocopy $repo $appDir /E /XD .git .venv __pycache__ .pytest_cache /XF *.pyc /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) {   # robocopy: 0-7 are success codes
    Write-Host "File copy failed (robocopy exit $LASTEXITCODE). Aborting install." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "$appDir\.venv")) {
    Invoke-Expression "$python -m venv `"$appDir\.venv`""
    Assert-Exit "Creating the Python environment"
}
& "$appDir\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
Assert-Exit "Upgrading pip"
& "$appDir\.venv\Scripts\python.exe" -m pip install "$appDir[voice,ui]" --quiet
Assert-Exit "Installing Jarvis dependencies"

# 3. Consent wizard — the employee decides what Jarvis may access
Write-Host ""
Write-Host "Choose what Jarvis may access on this machine:" -ForegroundColor Cyan
& "$appDir\.venv\Scripts\python.exe" -m jarvis setup
Assert-Exit "The consent wizard"

# 4. Claude API key
Write-Host ""
& "$appDir\.venv\Scripts\python.exe" -m jarvis secrets set anthropic
Assert-Exit "Storing the API key"

# 5. Autostart in the user session (never a Windows service - mic access)
if (-not $NoAutostart) {
    $pythonw = Join-Path $appDir ".venv\Scripts\pythonw.exe"
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "Jarvis" -Value "`"$pythonw`" -m jarvis" -Type String
    Write-Host "Autostart registered (HKCU Run key)." -ForegroundColor Green
}

Write-Host ""
Write-Host "Installed. Start now with:" -ForegroundColor Green
Write-Host "  & `"$appDir\.venv\Scripts\python.exe`" -m jarvis"
Write-Host "Change access decisions anytime:  ... -m jarvis setup"
Write-Host "Uninstall:  powershell -File `"$appDir\scripts\uninstall.ps1`""
