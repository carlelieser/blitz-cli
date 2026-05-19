# Blitz CLI Installer
# Run with: iwr -useb https://raw.githubusercontent.com/carlelieser/blitz-cli/main/install.ps1 | iex

$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:USERPROFILE ".blitz-cli"

function Write-Step { Write-Host "`n>> $args" -ForegroundColor Cyan }
function Write-Ok   { Write-Host "   $args" -ForegroundColor Green }
function Write-Warn { Write-Host "   $args" -ForegroundColor Yellow }
function Write-Fail { Write-Host "   $args" -ForegroundColor Red }

function Refresh-Path {
    $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
}

function Test-RealPython {
    try { return (& python --version 2>&1) -match "Python \d" }
    catch { return $false }
}

function Ensure-Dep {
    param($Label, $WingetId, [scriptblock]$Test)
    Write-Step "Checking $Label"
    if (& $Test) { Write-Ok "$Label already installed"; return }
    Write-Warn "$Label not found — installing via winget ..."
    winget install --id $WingetId --silent --accept-package-agreements --accept-source-agreements | Out-Null
    Refresh-Path
    if (-not (& $Test)) {
        Write-Fail "Failed to install $Label. Please install it manually and re-run."
        throw "$Label installation failed"
    }
    Write-Ok "$Label installed"
}

# ── Dependencies ──────────────────────────────────────────────────────────────

Ensure-Dep -Label "Python" -WingetId "Python.Python.3.12" -Test { Test-RealPython }
Ensure-Dep -Label "Node.js" -WingetId "OpenJS.NodeJS.LTS"  -Test { (Get-Command node -ErrorAction SilentlyContinue) -ne $null }

# ── Install blitz-cli ─────────────────────────────────────────────────────────

Write-Step "Installing blitz-cli"

$zip = Join-Path $env:TEMP "blitz-cli.zip"
$tmp = Join-Path $env:TEMP "blitz-cli-extract"

try {
    Invoke-WebRequest "https://github.com/carlelieser/blitz-cli/archive/refs/heads/main.zip" `
        -OutFile $zip -UseBasicParsing
} catch {
    throw "Download failed. Check your internet connection and try again."
}

if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip $tmp -Force
Remove-Item $zip -ErrorAction SilentlyContinue

if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
Copy-Item (Join-Path $tmp "blitz-cli-main") $InstallDir -Recurse
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Ok "Installed to $InstallDir"

# ── blitz.cmd shim ────────────────────────────────────────────────────────────

Write-Step "Creating blitz command"

$shim = "@echo off`r`npython `"%USERPROFILE%\.blitz-cli\blitz.py`" %*"
Set-Content (Join-Path $InstallDir "blitz.cmd") $shim -Encoding ASCII
Write-Ok "Blitz shim created"

# ── PATH ──────────────────────────────────────────────────────────────────────

Write-Step "Updating PATH"

$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$InstallDir*") {
    [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$InstallDir", "User")
    $env:PATH += ";$InstallDir"
    Write-Ok "Added $InstallDir to PATH"
} else {
    Write-Ok "Already on PATH — no changes needed"
}

# ── Run ───────────────────────────────────────────────────────────────────────

Write-Step "Patching Blitz"
& python (Join-Path $InstallDir "blitz.py")

Write-Host ""
Write-Host "  blitz              Re-download, install, and patch" -ForegroundColor DarkGray
Write-Host "  blitz patch        Patch existing Blitz installation" -ForegroundColor DarkGray
Write-Host "  blitz patch <file> Patch using a local installer"  -ForegroundColor DarkGray
Write-Host "  blitz update       Update blitz-cli itself"        -ForegroundColor DarkGray
Write-Host ""
