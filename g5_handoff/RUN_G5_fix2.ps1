# G5 runbook - fix2 (Windows / PowerShell)
# Prereqs: Windscribe connected; Python 3.11+ with `pip install --user asyncpg==0.32.0`;
#          aiven-ca.pem and g5_probe_fix2.py must sit next to this script.
# Portable: all paths derive from $PSScriptRoot. Never overwrites existing files.

$ErrorActionPreference = 'Stop'
$base = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $base) { throw 'Cannot determine script directory; run with: powershell -File <path>\RUN_G5_fix2.ps1' }

$ca     = Join-Path $base 'aiven-ca.pem'
$probe  = Join-Path $base 'g5_probe_fix2.py'
$audit  = Join-Path $base 'audit_perms.sql'
foreach ($p in @($ca, $probe)) { if (-not (Test-Path $p)) { throw "Missing file: $p" } }

# --- 0) Evidence: fingerprint of the probe actually executed ---
Write-Host "probe : $probe"
Get-FileHash $probe -Algorithm SHA256 | Format-List
Write-Host "ca    : $ca"
Get-FileHash $ca -Algorithm SHA256 | Format-List

# --- 1) Password (never echoed) ---
$secure = Read-Host "audit_ro password" -AsSecureString
$bstr  = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$pwd   = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

# --- 2) Env (process scope only) ---
$env:POSTGRES_SSLROOTCERT = $ca
$env:_G5_PW = $pwd
$env:AUDIT_DATABASE_URL = (python -c "import os,urllib.parse; print('postgresql://audit_ro:' + urllib.parse.quote(os.environ['_G5_PW'], safe='') + '@pg-2f0e4f1c-newsbot-db.e.aivencloud.com:27362/defaultdb?sslmode=require')")
Remove-Item Env:_G5_PW

# --- 3) G5 probe (real connection) ---
$reportPath = Join-Path $base 'g5_report.json'
if (Test-Path $reportPath) {
    $reportPath = Join-Path $base ("g5_report_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".json")
}
python $probe --dsn-env AUDIT_DATABASE_URL --report $reportPath
$probeExit = $LASTEXITCODE
Write-Host "Report written to $reportPath (probe exit=$probeExit)"

# --- 4) Optional read-only permission audit via psql (if installed) ---
# sslmode=require: libpq on Windows rejects Aiven's leaf cert because its IP SAN is
# malformed ("certificate contains IP address with invalid length 16"). Chain and
# hostname verification is performed by the probe itself (CA + check_hostname).
if ((Get-Command psql -ErrorAction SilentlyContinue) -and (Test-Path $audit)) {
    $env:PGPASSWORD = $pwd
    $caPath  = $ca.Replace('\','/')
    $outPath = Join-Path $base 'audit_out.txt'
    if (Test-Path $outPath) {
        $outPath = Join-Path $base ("audit_out_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".txt")
    }
    psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=audit_ro sslmode=require sslrootcert=$caPath" `
        -v ON_ERROR_STOP=1 -f $audit 2>&1 | Tee-Object -FilePath $outPath
    Remove-Item Env:PGPASSWORD
    Write-Host "psql output: $outPath"
}

# cleanup process env
Remove-Item Env:AUDIT_DATABASE_URL, Env:POSTGRES_SSLROOTCERT -ErrorAction SilentlyContinue
Write-Host "DONE (probe exit=$probeExit). Send the report (and audit_out.txt if produced) back for verdict."
