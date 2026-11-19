# =====================================================================
# RUN_G5_INVESTIGATE.ps1  (READ-ONLY investigation; NO production changes)
# =====================================================================
# Investigates independently:
#   (A) audit_ro.rolreplication = true
#   (B) avnadmin is a member of audit_ro (admin_option=true)
# Does NOT modify anything. Safe to run without approval.
# =====================================================================

$ErrorActionPreference = "Stop"
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$inv  = Join-Path $base 'INVESTIGATE_ROLREP_AND_MEMBERSHIP.sql'
$admin = Join-Path $base 'g5_admin.py'
$ca    = Join-Path $base 'aiven-ca.pem'

if (-not (Test-Path $inv))  { throw "Missing: $inv" }
if (-not (Test-Path $admin)) { throw "Missing: $admin" }
if (-not (Test-Path $ca))   { throw "Missing: $ca" }

# --- evidence: file hashes ---
Write-Host "== evidence: SHA-256 of the files actually used ==" -ForegroundColor Cyan
foreach ($f in @('g5_admin.py','INVESTIGATE_ROLREP_AND_MEMBERSHIP.sql','aiven-ca.pem')) {
    $p = Join-Path $base $f
    $h = Get-FileHash -Algorithm SHA256 $p
    Write-Host ("{0,-40} {1}" -f $h.Hash, $p)
}

# --- avnadmin password (masked) ---
$secure = Read-Host "avnadmin password" -AsSecureString
$plain  = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
              [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$env:AVNADMIN_PW = $plain

try {
    $env:POSTGRES_SSLROOTCERT = $ca
    Write-Host ""
    Write-Host "== INVESTIGATION (read-only, verify-full) ==" -ForegroundColor Cyan
    python $admin --mode preflight --sql $inv --ca $ca --user avnadmin 2>&1 | Tee-Object -FilePath (Join-Path $base ("investigate_out_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.txt'))
    $exit = $LASTEXITCODE
    Write-Host ""
    Write-Host "investigation exit=$exit" -ForegroundColor $(if ($exit -eq 0) { 'Green' } else { 'Red' })
    Write-Host "No production changes were made. Review the output above."
} finally {
    Remove-Item Env:AVNADMIN_PW, Env:POSTGRES_SSLROOTCERT -ErrorAction SilentlyContinue
    $plain = $null
}
EOF
echo "created"