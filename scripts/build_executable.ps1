param(
    [string]$Name = "EightfoldMigrationHelper"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error "PyInstaller is not installed. Run 'pip install pyinstaller' in your environment first."
}

$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $repoRoot "playwright-browsers"
if (-not (Test-Path $env:PLAYWRIGHT_BROWSERS_PATH)) {
    Write-Host "Downloading Playwright Chromium build into $env:PLAYWRIGHT_BROWSERS_PATH"
    python -m playwright install chromium
}

$specOutput = Join-Path $repoRoot "dist"
if (-not (Test-Path $specOutput)) {
    New-Item -ItemType Directory -Path $specOutput | Out-Null
}

pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name $Name `
    --add-data "$env:PLAYWRIGHT_BROWSERS_PATH;playwright-browsers" `
    --collect-all playwright `
    --collect-data certifi `
    migration_gui.py

Write-Host "Build complete. Executable located in dist\$Name.exe"
