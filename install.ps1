<#
.SYNOPSIS
    Installs BookScan Extractor: virtual environment, dependencies and a Start Menu
    shortcut.

.DESCRIPTION
    Deliberately no frozen executable. The application is installed from its
    repository, so updating it is a git pull rather than a rebuild and a
    redistribution. Run it again at any time: it is idempotent.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>

[CmdletBinding()]
param(
    [switch] $NoShortcut,
    [string] $ShortcutName = "BookScan Extractor"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Write-Step([string] $message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}

# --- uv ----------------------------------------------------------------------
# uv brings its own Python, so the machine does not need one already installed.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Step "Installing uv (it also manages the Python version)"
    $installer = "https://astral.sh/uv/install.ps1"
    Invoke-RestMethod -Uri $installer | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv could not be installed. Install it by hand from https://astral.sh/uv and run this script again."
    }
}
Write-Step "uv: $($uv.Source)"

# --- environment ---------------------------------------------------------------
Write-Step "Creating the virtual environment and installing the dependencies"
Push-Location $root
try {
    & uv sync
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

# --- a launcher that opens no console --------------------------------------------
# Every obvious candidate ends up showing a console window on Windows:
#   run.cmd            a .cmd file always brings its own cmd.exe window
#   pythonw.exe        in a uv virtual environment this is a trampoline compiled as a
#                      console program, which starts the console interpreter
#   pdfextract-gui.exe the windowed launcher from [project.gui-scripts], but it in
#                      turn runs that same console pythonw.exe
# venvwlauncher.exe is what the standard library copies into Scripts as pythonw.exe
# when it creates a virtual environment: a genuine windowed program that reads
# pyvenv.cfg to find its interpreter. It is copied here under a name of our own so
# that uv sync, which rewrites its own launchers, never replaces it.
Write-Step "Installing a console-free launcher"
$scripts = Join-Path $root ".venv\Scripts"
$launcher = Join-Path $scripts "pdfextract-launcher.exe"
$basePrefix = & (Join-Path $scripts "python.exe") -c "import sys; print(sys.base_prefix)"
$windowed = Join-Path $basePrefix "Lib\venv\scripts\nt\venvwlauncher.exe"
$launcherArguments = "-m pdfextract"

if (Test-Path $windowed) {
    Copy-Item $windowed $launcher -Force
}
if (-not (Test-Path $launcher)) {
    # Fall back on the windowed entry point. It shows a console on some machines,
    # but it is better than no shortcut at all.
    Write-Host "    venvwlauncher.exe not found, falling back on pdfextract-gui.exe"
    $launcher = Join-Path $scripts "pdfextract-gui.exe"
    $launcherArguments = ""
}
if (-not (Test-Path $launcher)) {
    throw "The environment looks incomplete: no launcher found in $scripts."
}
Write-Host "    $launcher"

# --- Start Menu shortcut --------------------------------------------------------
if (-not $NoShortcut) {
    Write-Step "Creating the Start Menu shortcut"
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    if (-not (Test-Path $startMenu)) { New-Item -ItemType Directory -Path $startMenu -Force | Out-Null }
    $linkPath = Join-Path $startMenu "$ShortcutName.lnk"
    $icon = Join-Path $root "assets\app.ico"

    # CreateShortcut loads an existing .lnk rather than starting from a blank one,
    # so every field has to be set, including the ones that are meant to be empty.
    # Leaving a stale Arguments behind is enough to break the shortcut silently.
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($linkPath)
    $shortcut.TargetPath = $launcher
    $shortcut.Arguments = $launcherArguments
    $shortcut.WorkingDirectory = $root
    $shortcut.WindowStyle = 1
    $shortcut.Description = "Extract page images from scanned book PDFs"
    $shortcut.IconLocation = if (Test-Path $icon) { $icon } else { $launcher }
    $shortcut.Save()
    Write-Host "    $linkPath"
}

Write-Step "Done"
Write-Host "Start it from the Start Menu, or with run.cmd."
Write-Host "Update it later with update.cmd, or from Help > Check for updates."
