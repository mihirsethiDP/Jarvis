# Create the Jarvis desktop shortcut - no terminal, no admin rights.
#
# The shortcut runs pythonw.exe (the windowless Python), which starts the
# assistant with no console at all and opens the orb in its own app window.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
#
# Removes it again with -Remove.

param(
    [switch]$Remove,
    [switch]$NoStartMenu
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent

$desktop = [Environment]::GetFolderPath("Desktop")
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$targets = @((Join-Path $desktop "Jarvis.lnk"))
if (-not $NoStartMenu) { $targets += (Join-Path $startMenu "Jarvis.lnk") }

if ($Remove) {
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "Removed $t" }
    }
    exit 0
}

# Prefer an installed copy (%LOCALAPPDATA%\Jarvis\app) over this checkout, so
# running the script from a clone still points the shortcut at the real install.
$installed = Join-Path $env:LOCALAPPDATA "Jarvis\app"
$root = if (Test-Path (Join-Path $installed ".venv\Scripts\pythonw.exe")) { $installed } else { $repo }

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe not found at $pythonw" -ForegroundColor Red
    Write-Host "Build the environment first: scripts\setup.ps1 (or scripts\install.ps1)."
    exit 1
}

$icon = Join-Path $root "jarvis\ui\static\jarvis.ico"
if (-not (Test-Path $icon)) {
    & (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "scripts\make_icon.py") | Out-Null
}

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $targets) {
    $parent = Split-Path $path -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = $pythonw
    $sc.Arguments = "-m jarvis --open-ui"
    $sc.WorkingDirectory = $root
    $sc.Description = "Jarvis - DigitalPaani voice assistant"
    if (Test-Path $icon) { $sc.IconLocation = $icon }
    $sc.Save()
    Write-Host "Created $path" -ForegroundColor Green
}

Write-Host ""
Write-Host "Double-click Jarvis to start. The orb opens in its own window;" -ForegroundColor Cyan
Write-Host "'Quit Jarvis' in that window stops it. No terminal involved."
