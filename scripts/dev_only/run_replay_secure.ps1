# scripts/dev_only/run_replay_secure.ps1
# Secure Phase Y-14 Replay execution script
# Strictly enforces credential security, Read-only session, and environment cleanup

$ErrorActionPreference = "Stop"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  Phase Y-14: Secure Replay Execution on Live Aiven PostgreSQL" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# 1. Prompt for password securely via Windows PowerShell (no echo)
$securePass = Read-Host "Enter Aiven PostgreSQL avnadmin Password" -AsSecureString
if (-not $securePass) {
    Write-Error "Password cannot be empty. Aborting."
    exit 1
}

$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePass)
$plainPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)

# Zero free the native BSTR pointer immediately
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

$dbUser = "avnadmin"
$dbHost = "pg-2f0e4f1c-newsbot-db.e.aivencloud.com"
$dbPort = "27362"
$dbName = "defaultdb"

try {
    # 2. Inject DATABASE_URL into current process session ONLY
    $env:DATABASE_URL = "postgresql+asyncpg://${dbUser}:${plainPass}@${dbHost}:${dbPort}/${dbName}?ssl=require"

    # Immediately erase plaintext password from memory
    $plainPass = $null
    [System.GC]::Collect()

    Write-Host "`n[1/3] Environment variable DATABASE_URL temporarily set for current process." -ForegroundColor Green
    Write-Host "[2/3] Launching audited Replay across 30 days..." -ForegroundColor Green

    # Run replay script using the dedicated virtual environment
    & .venv\Scripts\python.exe scripts/dev_only/replay_event_matching.py --days 30
}
catch {
    Write-Error "Replay execution halted with error: $_"
}
finally {
    # 3. Always clean up DATABASE_URL even on error or interrupt
    if (Test-Path Env:\DATABASE_URL) {
        Remove-Item Env:\DATABASE_URL
        Write-Host "`n[3/3] Successfully cleaned up: DATABASE_URL removed from environment." -ForegroundColor Yellow
    }
}
