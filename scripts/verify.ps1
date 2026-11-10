# scripts/verify.ps1 - Automated System Health and Verification Script

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  Middle East News Bot - System Health Verification " -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PipExe = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
$PytestExe = Join-Path $ProjectRoot ".venv\Scripts\pytest.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Virtual environment not found at $PythonExe. Please initialize .venv first."
    exit 1
}

# 1. PostgreSQL Connection Check
Write-Host "`n[1/4] Checking PostgreSQL Status..." -ForegroundColor Yellow
$PgReadyExe = "D:\pgsql\pgsql\bin\pg_isready.exe"
if (Test-Path $PgReadyExe) {
    $pgStatus = & $PgReadyExe -h 127.0.0.1 -p 5432
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  PostgreSQL is ready and accepting connections." -ForegroundColor Green
    } else {
        Write-Host "  WARNING: PostgreSQL is not reachable on 127.0.0.1:5432 (Exit code: $LASTEXITCODE)" -ForegroundColor Yellow
    }
} else {
    Write-Host "  pg_isready not found at $PgReadyExe, skipping binary check." -ForegroundColor Gray
}

# 2. Dependency Check (pip check)
Write-Host "`n[2/4] Running pip check (Dependency Validation)..." -ForegroundColor Yellow
$pipCheckOutput = & $PipExe check 2>&1
$pipExit = $LASTEXITCODE
if ($pipExit -eq 0) {
    Write-Host "  All dependencies are clean (exit code 0)." -ForegroundColor Green
} else {
    $otherConflicts = @()
    foreach ($line in $pipCheckOutput) {
        if ($line -match "aiogram 3.*has requirement pydantic<2\.12") {
            Write-Host "  [KNOWN ISSUE ISS-009] Recognized metadata conflict: $line" -ForegroundColor DarkYellow
        } else {
            $otherConflicts += $line
        }
    }
    if ($otherConflicts.Count -gt 0) {
        Write-Host "  CRITICAL: Unexpected dependency conflict detected!" -ForegroundColor Red
        $otherConflicts | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        exit 1
    } else {
        Write-Host "  No unexpected dependency conflicts found. (ISS-009 acknowledged)." -ForegroundColor Green
    }
}

# 3. Critical Module Imports Check
Write-Host "`n[3/4] Testing Critical Python Imports..." -ForegroundColor Yellow
$importScript = @"
import sys
modules = [
    'aiogram',
    'pydantic',
    'sqlalchemy',
    'asyncpg',
    'alembic',
    'google.genai',
    'feedparser',
    'trafilatura',
    'tenacity',
    'httpx'
]
for mod in modules:
    __import__(mod)
    print(f'  [OK] {mod}')
print('ALL_IMPORTS_SUCCESSFUL')
"@

$importResult = & $PythonExe -c $importScript 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Import check failed:" -ForegroundColor Red
    $importResult | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    exit 1
} else {
    $importResult | ForEach-Object { Write-Host "$_" -ForegroundColor Green }
}

# 4. Running Pytest Suite
Write-Host "`n[4/4] Running Pytest Suite..." -ForegroundColor Yellow
& $PytestExe -v --tb=short
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nPytest suite failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n====================================================" -ForegroundColor Green
Write-Host "  SUCCESS: All verification checks passed!          " -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Green
exit 0
