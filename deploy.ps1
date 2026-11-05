# Requires -Version 7.0
# deploy.ps1 — Single-click build & deployment pipeline for E.U.S.T.A.C.E.

$ErrorActionPreference = "Stop"
Clear-Host

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " 🚀 Starting E.U.S.T.A.C.E. Deployment Pipeline" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

# 1. Ensure we are in the correct project directory
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
Write-Host "📍 Workspace: $scriptDir" -ForegroundColor Gray

# 2. Check for virtual environment
if (-not (Test-Path ".\.venv")) {
    Write-Host "❌ Error: Virtual environment (.venv) not found! Run 'python -m venv .venv' first." -ForegroundColor Red
    exit 1
}

# 3. Synchronize Global Configurations (~/.eustace/)
$globalConfigDir = "$env:USERPROFILE\.eustace"
Write-Host "⚙️  Syncing global configurations to $globalConfigDir..." -ForegroundColor Blue

if (-not (Test-Path $globalConfigDir)) {
    New-Item -Path $globalConfigDir -ItemType Directory -Force | Out-Null
}

# Copy .env if it exists locally and doesn't exist globally
if (Test-Path ".env") {
    if (-not (Test-Path "$globalConfigDir\.env")) {
        Copy-Item ".env" "$globalConfigDir\.env" -Force
        Write-Host "   ✓ Copied local .env to global config." -ForegroundColor Green
    } else {
        Write-Host "   ✓ Global .env already exists. Skipping overwrite to preserve keys." -ForegroundColor Gray
    }
} else {
    Write-Host "   ⚠️  Warning: No local .env file found to sync!" -ForegroundColor Yellow
}

# Always copy system_prompt.md to ensure global persona is up to date
if (Test-Path "system_prompt.md") {
    Copy-Item "system_prompt.md" "$globalConfigDir\system_prompt.md" -Force
    Write-Host "   ✓ Synced system_prompt.md globally." -ForegroundColor Green
}

# 4. Clean old build artifacts
Write-Host "🧹 Cleaning old build files..." -ForegroundColor Blue
Remove-Item -Path build, dist -Recurse -Force -ErrorAction SilentlyContinue

# 5. Compile the standalone executable
Write-Host "🔨 Compiling standalone executable via PyInstaller..." -ForegroundColor Blue
$pyinstaller = ".\.venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $pyinstaller)) {
    Write-Host "   📦 PyInstaller not found. Installing inside .venv..." -ForegroundColor Yellow
    .\.venv\Scripts\pip.exe install pyinstaller
}

# Trigger compilation
& $pyinstaller --clean --onefile --name="eustace" `
    --collect-all docx `
    --collect-all pptx `
    --collect-all pypdf `
    --collect-all openpyxl `
    cli.py

# 6. Copy to active User PATH bin directory
$binDir = "$env:USERPROFILE\bin"
Write-Host "📦 Copying executable to $binDir..." -ForegroundColor Blue

if (-not (Test-Path $binDir)) {
    New-Item -Path $binDir -ItemType Directory -Force | Out-Null
}

# Force copy the compiled executable
Copy-Item "dist\eustace.exe" "$binDir\eustace.exe" -Force

# Verify PATH variable includes our bin folder
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$binDir*") {
    Write-Host "🗺️  Registering $binDir in Windows User PATH..." -ForegroundColor Yellow
    [Environment]::SetEnvironmentVariable("PATH", "$binDir;" + $userPath, "User")
    Write-Host "   ✓ PATH updated successfully. (Note: New terminals will pick this up)." -ForegroundColor Green
}

Write-Host "`n====================================================" -ForegroundColor Green
Write-Host " 🎉 E.U.S.T.A.C.E. Deployment Successful!" -ForegroundColor Green
Write-Host " You can now run 'eustace' globally from any folder." -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Green
