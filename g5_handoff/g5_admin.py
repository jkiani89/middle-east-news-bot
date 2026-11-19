#!/usr/bin/env python3
"""G5 maintenance-SQL runner with mandatory verify-full TLS.

Why this exists
---------------
Every production connection must validate the Aiven certificate chain AND the
hostname (libpq's `sslmode=verify-full`). Windows `psql` rejects Aiven's leaf
certificate because of a malformed IP SAN in it ("certificate contains IP
address with invalid length 16"); the same cluster verifies fine through the
Python/OpenSSL path used by the G5 probe. This runner uses exactly that path:
CERT_REQUIRED + check_hostname=True + the Aiven CA, i.e. verify-full. There is
no option to weaken it, and no sslmode=require code path at all.

Modes
-----
preflight  Read-only, double-locked:
             1. the session runs with default_transaction_read_only=on;
             2. the SQL file itself opens BEGIN TRANSACTION READ ONLY.
           Any write is refused by the server (SQLSTATE 25006).

fix        Refuses unless --apply-fix is passed. Before executing anything it
           validates every statement against a narrow allow-list (BEGIN, COMMIT,
           ROLLBACK, SET, DO, SELECT/WITH, and TEMPORARY grants to avnadmin or
           FROM PUBLIC). Any other DDL/DML aborts the run with nothing executed.
           The file's own DO blocks assert post-conditions inside the
           transaction, so a failed assert rolls the ACL back automatically.
           Every statement (DO bodies included) is scanned for destructive
           keywords before the allow-list runs, so a DO block cannot hide DDL.

Usage
-----
  python g5_admin.py --mode preflight --sql PREFLIGHT_AUDIT_RO_v3.sql \
      --ca aiven-ca.pem --user avnadmin
  python g5_admin.py --mode fix --apply-fix --sql FIX_AUDIT_RO_v3.sql \
      --ca aiven-ca.pem --user avnadmin

Password: $PGPASSWORD, or an interactive prompt (never echoed, never logged).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import os
import re
import ssl
import sys
from pathlib import Path

import asyncpg

DEFAULT_HOST = "pg-2f0e4f1c-newsbot-db.e.aivencloud.com"
DEFAULT_PORT = 27362
DEFAULT_DB = "defaultdb"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TLS = 3
EXIT_SQL = 4

QUERY_KEYWORDS = ("SELECT", "WITH", "TABLE", "VALUES", "SHOW", "EXPLAIN")
# Statements the MUST NOT appear in a maintenance file (defence in depth).
FORBIDDEN_WORDS = (
    "DROP", "TRUNCATE", "ALTER", "CREATE", "INSERT", "UPDATE", "DELETE",
    "MERGE", "COPY", "VACUUM", "REINDEX", "CLUSTER", "CALL",
)
WRITE_ALLOWLIST = re.compile(
    r"^(?:"
    r"BEGIN(?: TRANSACTION(?: READ ONLY)?)?"
    r"|COMMIT|END|ROLLBACK"
    r"|SET [A-Z_]+"
    r"|DO \$\w*\$"
    r"|GRANT TEMPORARY ON DATABASE \S+ TO \w+"
    r"|REVOKE TEMPORARY ON DATABASE \S+ FROM PUBLIC"
    r")$"
)


# --------------------------------------------------------------------------
# SQL file loading
# --------------------------------------------------------------------------
def split_sql(text: str) -> list[dict[str, str]]:
    """Split a psql script into echo markers and single SQL statements.

    Understands line/block comments, single and double quotes, and dollar
    quoting, so semicolons inside `DO $$ ... $$` blocks never split a statement.
    Returns [{"kind": "echo"|"sql", "text": ...}, ...].
    """
    items: list[dict[str, str]] = []
    buf: list[str] = []
    state = "normal"
    tag = ""
    depth = 0
    i = 0
    n = len(text)

    def flush() -> None:
        stmt = "".join(buf).strip()
        buf.clear()
        if stmt:
            items.append({"kind": "sql", "text": stmt})

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if state == "line_comment":
            if ch == "\n":
                state = "normal"
                buf.append(ch)
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                depth -= 1
                i += 2
                if depth == 0:
                    state = "normal"
                continue
            i += 1
            continue
        if state == "single":
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                state = "normal"
            i += 1
            continue
        if state == "double":
            buf.append(ch)
            if ch == '"' and nxt == '"':
                buf.append(nxt)
                i += 2
                continue
            if ch == '"':
                state = "normal"
            i += 1
            continue
        if state == "dollar":
            if text.startswith(tag, i):
                buf.append(tag)
                i += len(tag)
                state = "normal"
                continue
            buf.append(ch)
            i += 1
            continue

        # normal state
        if ch == "-" and nxt == "-":
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "block_comment"
            depth = 1
            i += 2
            continue
        if ch == "'":
            state = "single"
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            state = "double"
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[i:])
            if m:
                tag = m.group(0)
                buf.append(tag)
                i += len(tag)
                state = "dollar"
                continue
        if ch == "\\":
            # psql meta-command: only \echo is understood; flush pending SQL first.
            flush()
            end = text.find("\n", i)
            if end == -1:
                end = n
            line = text[i:end].strip()
            m = re.match(r"\\echo\s+(.*)$", line)
            if m:
                msg = m.group(1).strip()
                if len(msg) >= 2 and msg[0] == msg[-1] and msg[0] in "'\"":
                    msg = msg[1:-1]
                items.append({"kind": "echo", "text": msg})
            else:
                items.append({"kind": "echo", "text": f"[unsupported psql meta-command skipped] {line}"})
            i = end
            continue
        if ch == ";":
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1

    flush()
    return items


def first_keyword(stmt: str) -> str:
    head = stmt.lstrip()
    return re.split(r"[\s(]", head, maxsplit=1)[0].upper() if head else ""


def strip_literals(stmt: str) -> str:
    """Remove single-quoted string literals ('it''s' -> empty), keep code."""
    return re.sub(r"'(?:[^']|'')*'", "''", stmt)


def forbidden_keyword(stmt: str) -> str | None:
    """Return the first destructive keyword found in executable SQL, else None.

    Literals are stripped first so that RAISE message text such as
    'G5: PUBLIC unexpectedly has CREATE' is not mistaken for DDL. When the
    statement uses EXECUTE (dynamic SQL), the literals ARE the payload, so the
    original text is scanned as well.
    """
    code = strip_literals(stmt)
    upper = code.upper()
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{word}\b", upper):
            return word
    if re.search(r"\bEXECUTE\b", upper):
        original = stmt.upper()
        for word in FORBIDDEN_WORDS:
            if re.search(rf"\b{word}\b", original):
                return word
    return None


def validate_statements(items: list[dict[str, str]], mode: str) -> list[str]:
    """Return a list of policy violations (empty = the file may run)."""
    problems: list[str] = []
    write_statements = 0
    for item in items:
        if item["kind"] != "sql":
            continue
        stmt = item["text"]
        kw = first_keyword(stmt)
        flat = re.sub(r"\s+", " ", stmt).strip()

        if mode == "preflight":
            if kw not in QUERY_KEYWORDS and flat.upper() not in (
                "BEGIN TRANSACTION READ ONLY", "ROLLBACK",
            ) and not flat.upper().startswith("BEGIN TRANSACTION READ ONLY"):
                problems.append(f"preflight file contains a non-SELECT statement: {flat[:90]}")
            continue

        # fix mode
        # Explicit trip-wire for destructive keywords in EVERY statement, before
        # any allow-list shortcut: a `DO $$ ... DROP TABLE ... $$` block would
        # otherwise pass the keyword allow-list while hiding DDL.
        forbidden_hit = forbidden_keyword(stmt)
        if forbidden_hit:
            problems.append(
                f"fix file mentions forbidden keyword {forbidden_hit}: {flat[:90]}"
            )
            continue

        if kw in QUERY_KEYWORDS:
            continue
        if WRITE_ALLOWLIST.match(flat):
            write_statements += 1
            continue
        if kw == "DO" and flat.startswith("DO $"):
            write_statements += 1
            continue
        problems.append(f"fix file contains a statement outside the allow-list: {flat[:90]}")

    if mode == "fix" and not any("REVOKE TEMPORARY" in i["text"].upper() for i in items if i["kind"] == "sql"):
        problems.append("fix file has no REVOKE TEMPORARY statement - refused")
    if mode == "fix" and write_statements == 0:
        problems.append("fix file has no write statement - refused")
    return problems


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------
def build_ssl_context(cafile: str) -> ssl.SSLContext:
    """verify-full: chain validated against the Aiven CA, hostname checked."""
    path = Path(cafile)
    if not path.is_file():
        raise SystemExit(f"[G5] CA file not found: {path} (verify-full cannot be satisfied)")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(path))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if not (ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED):
        raise SystemExit("[G5] TLS context is not verify-full; refusing to connect")
    return ctx


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def _fmt_rows(records) -> str:
    if not records:
        return "  (0 rows)"
    cols = list(records[0].keys())
    rows = [[("" if r[c] is None else str(r[c])) for c in cols] for r in records]
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    out = ["  " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)),
           "  " + "-+-".join("-" * w for w in widths)]
    out += ["  " + " | ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


async def execute_items(conn, items, echo=print, verbose_sql=False) -> int:
    """Run parsed statements in order; return the number of statements executed."""
    count = 0
    for item in items:
        if item["kind"] == "echo":
            echo(f"\n{item['text']}")
            continue
        stmt = item["text"]
        kw = first_keyword(stmt)
        if verbose_sql:
            one_line = re.sub(r"\s+", " ", stmt)[:160]
            echo(f"SQL> {one_line}")
        if kw in QUERY_KEYWORDS:
            echo(_fmt_rows(await conn.fetch(stmt)))
        else:
            status = await conn.execute(stmt)
            if status:
                echo(f"  {status}")
        count += 1
    return count


async def run(args) -> int:
    sql_path = Path(args.sql)
    if not sql_path.is_file():
        print(f"[G5] SQL file not found: {sql_path}", file=sys.stderr)
        return EXIT_USAGE

    items = split_sql(sql_path.read_text(encoding="utf-8"))
    problems = validate_statements(items, args.mode)
    if problems:
        print("[G5] REFUSED - the SQL file does not satisfy the mode policy:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_USAGE

    ctx = build_ssl_context(args.ca)
    ca_sha = hashlib.sha256(Path(args.ca).read_bytes()).hexdigest()
    print(f"[G5] mode={args.mode}  sql={sql_path.name}  statements={sum(1 for i in items if i['kind']=='sql')}")
    print(f"[G5] TLS: verify-full (CERT_REQUIRED + check_hostname)  ca_sha256={ca_sha}")

    if args.mode == "fix" and not args.apply_fix:
        print("[G5] DRY RUN: --apply-fix not passed. Statements that WOULD run:")
        for item in items:
            if item["kind"] == "sql":
                print("  " + re.sub(r"\s+", " ", item["text"])[:160])
        print("[G5] Nothing connected, nothing executed.")
        return EXIT_OK

    password = os.environ.get("PGPASSWORD") or getpass.getpass(f"{args.user} password: ")
    try:
        conn = await asyncpg.connect(
            host=args.host, port=args.port, user=args.user,
            password=password, database=args.db, ssl=ctx,
            timeout=30, server_settings={"application_name": "g5_admin"},
        )
    except (ssl.SSLError, ssl.SSLCertVerificationError) as exc:
        print(f"[G5] TLS verification failed: {exc}", file=sys.stderr)
        return EXIT_TLS
    except Exception as exc:  # connection-level
        print(f"[G5] connection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_TLS

    def _on_log(_conn, message) -> None:
        # Surface server NOTICEs (the DO-block guards/asserts speak through them).
        print(f"[pg:{message.severity}] {message.message}")

    try:
        conn.add_log_listener(_on_log)
        ident = await conn.fetchrow(
            "SELECT current_user, session_user, current_database() AS db,"
            "       current_setting('server_version') AS version,"
            "       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl,"
            "       (SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS tls"
        )
        print(f"[G5] connected as {ident['current_user']} (session_user={ident['session_user']}) "
              f"db={ident['db']} server={ident['version']} ssl={ident['ssl']} tls={ident['tls']}")
        if ident["ssl"] is not True:
            print("[G5] REFUSED: connection is not TLS-encrypted.", file=sys.stderr)
            return EXIT_TLS
        if args.mode == "fix" and ident["current_user"] != "avnadmin":
            print(f"[G5] REFUSED: fix must run as avnadmin, got {ident['current_user']}.", file=sys.stderr)
            return EXIT_USAGE
        if args.mode == "preflight":
            await conn.execute("SET default_transaction_read_only = on")
            ro = await conn.fetchval("SELECT current_setting('default_transaction_read_only')")
            print(f"[G5] guard: default_transaction_read_only={ro} (server rejects writes)")
            if ro != "on":
                print("[G5] REFUSED: could not enable the read-only guard.", file=sys.stderr)
                return EXIT_USAGE

        executed = 0
        try:
            executed = await execute_items(conn, items, verbose_sql=args.verbose)
            print(f"\n[G5] completed {executed} statements")
            return EXIT_OK
        except asyncpg.PostgresError as exc:
            print(f"\n[G5] SQL error: {type(exc).__name__}: {exc}", file=sys.stderr)
            if args.mode == "fix":
                # Only statements up to the failure ran; a COMMIT before it would
                # mean the change is already durable and must NOT be reported as
                # rolled back.
                committed = any(
                    first_keyword(i["text"]) in ("COMMIT", "END")
                    for i in [s for s in items if s["kind"] == "sql"][:executed]
                )
                if committed:
                    print("[G5] WARNING: a COMMIT already succeeded before this error;"
                          " the ACL change IS COMMITTED. Re-run the preflight.", file=sys.stderr)
                else:
                    print("[G5] the transaction was aborted by the server; attempting ROLLBACK.", file=sys.stderr)
                    try:
                        await conn.execute("ROLLBACK")
                        print("[G5] rolled back - no ACL change was committed.", file=sys.stderr)
                    except asyncpg.PostgresError as rb_exc:
                        print(f"[G5] rollback reported: {rb_exc}", file=sys.stderr)
            return EXIT_SQL
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="G5 maintenance SQL runner (verify-full TLS only)")
    p.add_argument("--mode", required=True, choices=("preflight", "fix"))
    p.add_argument("--sql", required=True, help="SQL file to execute")
    p.add_argument("--ca", default="aiven-ca.pem", help="Aiven CA bundle (required for verify-full)")
    p.add_argument("--user", default="avnadmin")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--apply-fix", action="store_true", help="actually run the fix file (default: dry run)")
    p.add_argument("--verbose", action="store_true", help="also print each statement before running it")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
