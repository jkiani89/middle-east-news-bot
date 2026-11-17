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

# Zero-free the unmanaged BSTR allocation immediately
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

# URL-encode the password to safely handle special characters (@, :, /, %, etc.)
$encodedPass = [System.Uri]::EscapeDataString($plainPass)

# Discard plaintext variable reference
$plainPass = $null

$dbUser = "avnadmin"
$dbHost = "pg-2f0e4f1c-newsbot-db.e.aivencloud.com"
$dbPort = "27362"
$dbName = "defaultdb"

# Memory residual note:
# ZeroFreeBSTR clears the unmanaged BSTR allocation. However, in managed .NET runtimes,
# System.String instances (like $encodedPass and the process environment block) remain
# in managed memory until collected by CLR. Additionally, the spawned python.exe process
# inherits DATABASE_URL in its process environment space. This script prevents persistent
# disk storage and clears environment references on exit, but cannot alter the operating
# system memory isolation model.

$pythonExitCode = 0

try {
    # 2. Inject DATABASE_URL into current process session ONLY (Never logged or printed)
    $env:DATABASE_URL = "postgresql+asyncpg://${dbUser}:${encodedPass}@${dbHost}:${dbPort}/${dbName}?ssl=require"
    $encodedPass = $null

    Write-Host "`n[1/3] Environment variable DATABASE_URL temporarily set for current process." -ForegroundColor Green
    Write-Host "[2/3] Launching audited Replay across 30 days..." -ForegroundColor Green

    # Run replay script using the dedicated virtual environment
    & .venv\Scripts\python.exe scripts/dev_only/replay_event_matching.py --days 30
    $pythonExitCode = $LASTEXITCODE

    if ($pythonExitCode -ne 0) {
        Write-Host "`n[!] Python execution completed with non-zero exit code: $pythonExitCode" -ForegroundColor Red
    }
}
catch {
    Write-Host "`n[!] Replay execution halted with error: $_" -ForegroundColor Red
    if ($pythonExitCode -eq 0) { $pythonExitCode = 1 }
}
finally {
    # 3. Always clean up DATABASE_URL even on error or Ctrl+C
    # Limitation: If the PowerShell host process is forcibly terminated (e.g. killed via Task Manager
    # or TerminateProcess), 'finally' will not execute. However, $env: is process-scoped only and
    # dies with the process; it is never persisted to Machine or User registry.
    if (Test-Path Env:\DATABASE_URL) {
        Remove-Item Env:\DATABASE_URL
        Write-Host "`n[3/3] Successfully cleaned up: DATABASE_URL removed from environment." -ForegroundColor Yellow
    }
}

if ($pythonExitCode -ne 0) {
    exit $pythonExitCode
}
