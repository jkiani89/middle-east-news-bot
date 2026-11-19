# G5 maintenance runbook - fix4 (Windows / PowerShell)
# =====================================================================
# Two steps, both over verify-full TLS (CA chain + hostname):
#   1. preflight  - READ-ONLY inventory. Runs by default. Changes nothing.
#   2. fix        - REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC.
#                   Runs ONLY with -ApplyFix, ONLY if preflight was clean in
#                   this same invocation, and ONLY after you type the phrase.
#
# Everything goes through g5_admin.py, which refuses any TLS setting weaker
# than verify-full and refuses any statement outside a narrow allow-list.
#
# Prereqs: Windscribe connected; Python 3.11+ with asyncpg (`pip install --user
#          asyncpg==0.32.0`); aiven-ca.pem, g5_admin.py and the two .sql files
#          next to this script.
# =====================================================================

param(
    [switch]$ApplyFix
)

# Native commands report failures through $LASTEXITCODE; every critical step is
# checked explicitly below and stops the script with `throw`.
$ErrorActionPreference = 'Continue'

$base = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $base) { throw 'Cannot determine script directory.' }

$ca        = Join-Path $base 'aiven-ca.pem'
$admin     = Join-Path $base 'g5_admin.py'
$preflight = Join-Path $base 'PREFLIGHT_AUDIT_RO_v3.sql'
$fix       = Join-Path $base 'FIX_AUDIT_RO_v3.sql'
$stamp     = Get-Date -Format 'yyyyMMdd_HHmmss'

foreach ($p in @($ca, $admin, $preflight)) { if (-not (Test-Path $p)) { throw "Missing file: $p" } }
if ($ApplyFix -and -not (Test-Path $fix)) { throw "Missing file: $fix" }

Write-Host "== evidence: SHA-256 of the files actually used =="
foreach ($p in @($admin, $preflight, $fix)) {
    if (Test-Path $p) { Get-FileHash $p -Algorithm SHA256 | Format-List }
}

# --- password: process env only, never echoed, always cleared ---
$secure = Read-Host "avnadmin password" -AsSecureString
$bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

try {
    # ---------------- STEP 1: PREFLIGHT (read-only, server-enforced) ----------------
    $pfOut = Join-Path $base ("preflight_out_$stamp.txt")
    Write-Host ""
    Write-Host "== STEP 1: PREFLIGHT (read-only) =="
    python $admin --mode preflight --sql $preflight --ca $ca --user avnadmin 2>&1 |
        Tee-Object -FilePath $pfOut
    $pfExit = $LASTEXITCODE
    Write-Host "preflight exit=$pfExit  output=$pfOut"
    if ($pfExit -ne 0) {
        throw "Preflight failed (exit $pfExit). STOP: do not run any fix. Read the output above."
    }

    # ---------------- automated stop criteria ----------------
    $text = Get-Content $pfOut -Raw
    $loses = @()
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\s*(\S+)\s*\|.*\|\s*LOSES\s*$') { $loses += $Matches[1] }
    }
    $unexpected = @($loses | Where-Object { $_ -ne 'audit_ro' })

    Write-Host ""
    Write-Host ("roles classified LOSES: " + $(if ($loses.Count) { $loses -join ', ' } else { '<none>' }))
    if ($unexpected.Count -gt 0) {
        Write-Host ('STOP: these roles would also lose TEMPORARY -> ' + ($unexpected -join ', ')) -ForegroundColor Red
        Write-Host "Do NOT run the fix. Send the preflight output to the project owner." -ForegroundColor Red
        throw "Stop criterion triggered: unexpected role(s) affected."
    }
    if ($loses.Count -eq 0) {
        Write-Host "NOTE: no role is classified LOSES - the finding may already be remediated." -ForegroundColor Yellow
    }
    if ($text -notmatch 'default_transaction_read_only=on') {
        Write-Host "STOP: the read-only guard was not confirmed by the runner." -ForegroundColor Red
        throw "Stop criterion triggered: read-only state not proven."
    }
    if ($text -notmatch 'verify-full \(CERT_REQUIRED \+ check_hostname\)') {
        Write-Host "STOP: verify-full TLS was not reported by the runner." -ForegroundColor Red
        throw "Stop criterion triggered: TLS level not proven."
    }

    Write-Host ""
    Write-Host "MANUAL CHECKS before any approval (sections 3b/3c/4/5/6 of the output):" -ForegroundColor Cyan
    Write-Host "  [ ] 3c shows LOSES only for audit_ro"
    Write-Host "  [ ] 3b shows no membership edge that gives another login role TEMP via SET ROLE"
    Write-Host "  [ ] 5 shows temp_relations = 0 (no session is mid-work)"
    Write-Host "  [ ] 6 datacl_baseline is {=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}"
    Write-Host "  [ ] any unknown role or dependency -> STOP, report, do not continue"

    if (-not $ApplyFix) {
        Write-Host ""
        Write-Host "Preflight recorded. No ACL change was made." -ForegroundColor Green
        Write-Host "To apply the approved fix later, re-run with -ApplyFix AFTER human approval."
        return
    }

    # ---------------- STEP 2: FIX (gated) ----------------
    Write-Host ""
    Write-Host "== STEP 2: FIX ==" -ForegroundColor Yellow
    Write-Host "Exactly one ACL mutation: REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC"
    $answer = Read-Host "Type exactly  APPLY TEMPORARY REVOKE  to continue"
    if ($answer -cne 'APPLY TEMPORARY REVOKE') {
        Write-Host "Aborted by operator. Nothing executed." -ForegroundColor Yellow
        return
    }

    $fixOut = Join-Path $base ("fix_out_$stamp.txt")
    python $admin --mode fix --apply-fix --sql $fix --ca $ca --user avnadmin --verbose 2>&1 |
        Tee-Object -FilePath $fixOut
    $fixExit = $LASTEXITCODE
    Write-Host "fix exit=$fixExit  output=$fixOut"
    if ($fixExit -ne 0) {
        throw "Fix failed (exit $fixExit). If the log mentions rollback, no ACL change was committed."
    }

    Write-Host ""
    Write-Host "== STEP 3: RE-AUDIT ==" -ForegroundColor Green
    Write-Host "Run the G5 probe again:"
    Write-Host "  & (Join-Path `$PSScriptRoot 'RUN_G5_REAUDIT_fix4.ps1')"
    Write-Host "Expected: 3-4 = PASS and verdict = PASS."
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
