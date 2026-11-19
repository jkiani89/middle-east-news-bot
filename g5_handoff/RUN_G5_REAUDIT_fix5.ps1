# G5 re-audit runbook - fix5 (Windows / PowerShell)
# =====================================================================
# Runs ONLY the G5 probe (g5_probe_fix3.py) as audit_ro, over verify-full TLS:
# the probe validates the Aiven CA chain and the hostname itself
# (CERT_REQUIRED + check_hostname) regardless of the DSN's sslmode string,
# which is why the DSN below carries sslmode=verify-full.
#
# No psql, no write path, no ACL change. The probe runs inside
# BEGIN / SET TRANSACTION READ ONLY / ROLLBACK.
#
# Prereqs: Windscribe connected; Python 3.11+ with asyncpg; aiven-ca.pem and
#          g5_probe_fix3.py next to this script.
# =====================================================================

$ErrorActionPreference = 'Continue'
$base = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $base) { throw 'Cannot determine script directory.' }

$ca    = Join-Path $base 'aiven-ca.pem'
$probe = Join-Path $base 'g5_probe_fix3.py'
foreach ($p in @($ca, $probe)) { if (-not (Test-Path $p)) { throw "Missing file: $p" } }

Write-Host "== evidence: SHA-256 of the probe actually executed =="
Get-FileHash $probe -Algorithm SHA256 | Format-List
Get-FileHash $ca -Algorithm SHA256 | Format-List

# --- password: process env only, never echoed, always cleared ---
$secure = Read-Host "audit_ro password" -AsSecureString
$bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$pw     = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

try {
    $env:POSTGRES_SSLROOTCERT = $ca
    $env:_G5_PW = $pw
    $env:AUDIT_DATABASE_URL = (python -c "import os,urllib.parse; print('postgresql://audit_ro:' + urllib.parse.quote(os.environ['_G5_PW'], safe='') + '@pg-2f0e4f1c-newsbot-db.e.aivencloud.com:27362/defaultdb?sslmode=verify-full')")

    $reportPath = Join-Path $base ("g5_report_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".json")
    python $probe --dsn-env AUDIT_DATABASE_URL --report $reportPath
    $probeExit = $LASTEXITCODE
    Write-Host "Report written to $reportPath (probe exit=$probeExit)"

    if (Test-Path $reportPath) {
        $j = Get-Content $reportPath -Raw | ConvertFrom-Json
        Write-Host ""
        Write-Host "probe_version : $($j.probe_version)"
        Write-Host "verdict       : $($j.verdict)"
        if ($j.per_section) { $j.per_section | Format-List }
        if ($j.error_message) { Write-Host "ERROR: $($j.error_message)" }
        Write-Host ""
        Write-Host "Expected after the approved fix: 3-4 = PASS and verdict = PASS."
    }
}
finally {
    Remove-Item Env:AUDIT_DATABASE_URL, Env:POSTGRES_SSLROOTCERT, Env:_G5_PW -ErrorAction SilentlyContinue
}
