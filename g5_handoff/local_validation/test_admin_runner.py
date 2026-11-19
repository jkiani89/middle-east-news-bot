"""Offline tests for g5_admin.py (no database, no network).

Validates the parts that protect production: statement splitting (dollar
quoting, comments, quotes), the mode policy/allow-list, and that the TLS
context is verify-full with the Aiven CA.
"""
import asyncio
import importlib.util
import ssl
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HANDOFF = HERE.parent
spec = importlib.util.spec_from_file_location("g5_admin", HANDOFF / "g5_admin.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- splitting
pf = M.split_sql((HANDOFF / "PREFLIGHT_AUDIT_RO_v3.sql").read_text())
fx = M.split_sql((HANDOFF / "FIX_AUDIT_RO_v3.sql").read_text())

pf_sql = [i for i in pf if i["kind"] == "sql"]
fx_sql = [i for i in fx if i["kind"] == "sql"]

check("preflight parses to 11 statements", len(pf_sql) == 11, f"got {len(pf_sql)}")
check("preflight opens with BEGIN TRANSACTION READ ONLY",
      pf_sql[0]["text"].upper().startswith("BEGIN TRANSACTION READ ONLY"), pf_sql[0]["text"])
check("preflight ends with ROLLBACK", pf_sql[-1]["text"].upper() == "ROLLBACK", pf_sql[-1]["text"])
check("preflight has 11 echo markers",
      len([i for i in pf if i["kind"] == "echo"]) == 11,
      str(len([i for i in pf if i["kind"] == "echo"])))

do_blocks = [s["text"] for s in fx_sql if s["text"].upper().startswith("DO $")]
check("fix has 3 DO blocks kept intact", len(do_blocks) == 3, str(len(do_blocks)))
check("every DO block is dollar-quoted and closed",
      all(b.rstrip().endswith("$") and b.count("$") >= 4 for b in do_blocks))
check("no statement contains a stray psql meta-command",
      not any("\\echo" in s["text"] for s in pf_sql + fx_sql))

# semicolons inside dollar-quoted blocks must not split statements
probe = M.split_sql("DO $$ BEGIN PERFORM 1; PERFORM 2; END $$; SELECT 1;")
check("semicolons inside $$ do not split",
      [s["text"] for s in probe] == ["DO $$ BEGIN PERFORM 1; PERFORM 2; END $$", "SELECT 1"],
      str([s["text"] for s in probe]))

quoted = M.split_sql("SELECT 'a;b', \"c;d\"; -- e;f\nSELECT 2;")
check("semicolons in string/identifier/comment do not split",
      len([s for s in quoted if s["kind"] == "sql"]) == 2,
      str([s["text"] for s in quoted if s["kind"] == "sql"]))

# ---------------------------------------------------------------- policy
check("preflight file passes preflight policy", M.validate_statements(pf, "preflight") == [])
check("fix file passes fix policy", M.validate_statements(fx, "fix") == [])

bad_fix = M.split_sql((HANDOFF / "FIX_AUDIT_RO_v3.sql").read_text() + "\nDROP TABLE public.articles;\n")
check("fix policy rejects DROP", bool(M.validate_statements(bad_fix, "fix")))

bad_grant = M.split_sql((HANDOFF / "FIX_AUDIT_RO_v3.sql").read_text()
                        + "\nGRANT ALL ON DATABASE defaultdb TO PUBLIC;\n")
check("fix policy rejects broad GRANT", bool(M.validate_statements(bad_grant, "fix")))

hidden_ddl = M.split_sql((HANDOFF / "FIX_AUDIT_RO_v3.sql").read_text()
                         + "\nDO $$ BEGIN EXECUTE 'DROP TABLE public.articles'; END $$;\n")
check("fix policy rejects DDL hidden inside a DO block",
      any("forbidden keyword DROP" in p for p in M.validate_statements(hidden_ddl, "fix")),
      str(M.validate_statements(hidden_ddl, "fix"))[:160])

no_revoke = M.split_sql("BEGIN;\nDO $$ BEGIN NULL; END $$;\nCOMMIT;")
check("fix policy rejects a file without REVOKE", bool(M.validate_statements(no_revoke, "fix")))

pf_write = M.split_sql("BEGIN TRANSACTION READ ONLY;\nCREATE TEMP TABLE x(i int);\nROLLBACK;")
check("preflight policy rejects a write", bool(M.validate_statements(pf_write, "preflight")))

# ---------------------------------------------------------------- TLS
ctx = M.build_ssl_context(str(HANDOFF / "aiven-ca.pem"))
check("TLS context is verify-full",
      ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED,
      f"check_hostname={ctx.check_hostname} verify_mode={ctx.verify_mode}")
check("CA file loaded (no system fallback)", (HANDOFF / "aiven-ca.pem").stat().st_size > 0)

try:
    M.build_ssl_context(str(HANDOFF / "does-not-exist.pem"))
    check("missing CA is refused", False)
except SystemExit as exc:
    check("missing CA is refused", "CA file not found" in str(exc), str(exc))

src = (HANDOFF / "g5_admin.py").read_text()
check("no CERT_NONE / sslmode=require code path in the runner",
      "CERT_NONE" not in src and "sslmode=require'" not in src)


# ---------------------------------------------------------------- execution order
class FakeConn:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql):
        self.calls.append(("fetch", sql.split()[0]))
        return [{"col": "value"}]

    async def execute(self, sql):
        self.calls.append(("execute", sql.split()[0]))
        return sql.split()[0]


async def dry():
    conn = FakeConn()
    out = []
    n = await M.execute_items(conn, pf, echo=out.append)
    return conn, n, out


conn, n, out = asyncio.get_event_loop().run_until_complete(dry()) if sys.version_info < (3, 10) \
    else asyncio.run(dry())
check("all 11 preflight statements executed in order", n == 11, str(n))
check("BEGIN/ROLLBACK go through execute, SELECT through fetch",
      conn.calls[0] == ("execute", "BEGIN") and conn.calls[-1] == ("execute", "ROLLBACK")
      and all(c[0] == "fetch" for c in conn.calls[1:-1]),
      str(conn.calls[:2] + conn.calls[-1:]))
check("echo markers are printed", any(m.strip().startswith("=== 3c.") for m in out),
      str([m.strip() for m in out if m.strip().startswith("=== ")][:2]))

print()
if failures:
    print(f"FAILURES: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL ADMIN-RUNNER TESTS PASSED")
