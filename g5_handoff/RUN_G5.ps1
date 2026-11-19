# G5 runbook - Windows (PowerShell)
# Prereqs: Windscribe connected; Python 3.11+ with `pip install asyncpg==0.32.0`;
#          aiven-ca.pem and g5_probe.py must sit next to this script.
# Portable: all paths derive from $PSScriptRoot. Never overwrites existing files.

$ErrorActionPreference = 'Stop'
# Resolve base dir from script location (portable, non-destructive).
$base = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $base) { $base = 'C:\g5' }

if (-not (Test-Path (Join-Path $base 'aiven-ca.pem'))) { throw "Missing CA in $base" }
if (-not (Test-Path (Join-Path $base 'g5_probe.py')))  { throw "Missing probe in $base" }

# --- 1) Password (never echoed) ---
$secure = Read-Host "audit_ro password" -AsSecureString
$bstr  = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$pwd   = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

# --- 2) Env (process scope only) ---
$env:POSTGRES_SSLROOTCERT = (Join-Path $base 'aiven-ca.pem')
# URL-encode the password so special characters cannot break DSN parsing.
$env:_G5_PW = $pwd
$env:AUDIT_DATABASE_URL = (python -c "import os,urllib.parse; print('postgresql://audit_ro:' + urllib.parse.quote(os.environ['_G5_PW'], safe='') + '@pg-2f0e4f1c-newsbot-db.e.aivencloud.com:27362/defaultdb?sslmode=require')")
Remove-Item Env:_G5_PW

# --- 3) G5 probe (real connection) ---
# Non-destructive output name: never overwrite an existing report.
$reportPath = Join-Path $base 'g5_report.json'
if (Test-Path $reportPath) {
    $reportPath = Join-Path $base ("g5_report_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".json")
}
python (Join-Path $base 'g5_probe.py') --dsn-env AUDIT_DATABASE_URL --report $reportPath
Write-Host "Report written to $reportPath"

# --- 4) Optional read-only permission audit via psql (if installed) ---
if (Get-Command psql -ErrorAction SilentlyContinue) {
    $env:PGPASSWORD = $pwd
    $caPath = (Join-Path $base 'aiven-ca.pem').Replace('\','/')
    $outPath = Join-Path $base 'audit_out.txt'
    if (Test-Path $outPath) {
        $outPath = Join-Path $base ("audit_out_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".txt")
    }
    psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=audit_ro sslmode=verify-full sslrootcert=$caPath" `
        -v ON_ERROR_STOP=1 -f (Join-Path $base 'audit_perms.sql') 2>&1 | Tee-Object -FilePath $outPath
    Remove-Item Env:PGPASSWORD
}

# cleanup process env
Remove-Item Env:AUDIT_DATABASE_URL, Env:POSTGRES_SSLROOTCERT -ErrorAction SilentlyContinue
Write-Host "DONE. Send g5_report.json (and audit_out.txt if produced) back for verdict."
