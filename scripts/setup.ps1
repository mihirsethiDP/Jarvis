# Jarvis developer setup for Windows.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== Jarvis setup ==" -ForegroundColor Cyan

# Find a suitable Python (3.10+)
$python = $null
foreach ($candidate in @("py -3.12", "py -3.11", "py -3.10", "python")) {
    try {
        $version = Invoke-Expression "$candidate --version" 2>$null
        if ($version -match "Python 3\.(1[0-9])") { $python = $candidate; break }
    } catch {}
}
if (-not $python) {
    Write-Host "Python 3.10+ not found. Install it first: winget install Python.Python.3.12" -ForegroundColor Red
    exit 1
}
Write-Host "Using: $python ($(Invoke-Expression "$python --version"))"

if (-not (Test-Path ".venv")) {
    Invoke-Expression "$python -m venv .venv"
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[voice,ui,dev]"

Write-Host ""
Write-Host "Setup complete. Next steps:" -ForegroundColor Green
Write-Host "  1. .venv\Scripts\Activate.ps1"
Write-Host "  2. jarvis secrets set anthropic      # store your Claude API key"
Write-Host "  3. jarvis --text                     # try it without a microphone"
Write-Host "  4. jarvis                            # full voice mode"
