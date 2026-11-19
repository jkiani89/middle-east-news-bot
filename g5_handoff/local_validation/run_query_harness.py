#!/usr/bin/env python3
"""Dev-only harness: run a probe module's own QUERIES + evaluate() + overall()
against a local throwaway PostgreSQL, as a non-superuser, in a read-only txn.

Purpose: validate the criteria logic of g5_probe_fix3.py against a real server
without touching Aiven. TLS is NOT used here (the local test build has no
OpenSSL); the TLS gate in main() is unchanged code and is exercised on the real
audited cluster instead.

Usage: run_query_harness.py <probe.py> <dsn> [label]
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import asyncpg

ORDER = ("identity", "event_trigger_visibility", "transaction_read_only", "1A", "1B",
         "1C", "1D", "1E", "2", "3-4", "3-4b", "3-6", "3-7", "3-7b", "3-8", "3-8-count")


def load_module(path: str):
    spec = importlib.util.spec_from_file_location("probe_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def run(mod, dsn: str) -> dict:
    conn = await asyncpg.connect(dsn=dsn)
    ev: dict = {}
    tx = None
    try:
        for key in ("identity", "event_trigger_visibility"):
            ev[key] = [dict(r) for r in await conn.fetch(mod.assert_safe(key))]
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SET TRANSACTION READ ONLY")
        for key in ORDER[2:]:
            ev[key] = [dict(r) for r in await conn.fetch(mod.assert_safe(key))]
        ev["transaction_read_only_after"] = [
            dict(r) for r in await conn.fetch(mod.assert_safe("transaction_read_only"))
        ]
        per = mod.evaluate(ev)
        return {"per_section": per, "verdict": mod.overall(per), "sections": ev,
                "probe_version": getattr(mod, "PROBE_VERSION", "(none)")}
    finally:
        if tx is not None:
            try:
                await tx.rollback()
            except Exception:
                pass
        await conn.close()


def main() -> int:
    path, dsn = sys.argv[1], sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else path
    mod = load_module(path)
    out = asyncio.run(run(mod, dsn))
    json.dump(out, open(f"/tmp/g5_harness_{label}.json", "w"), indent=2, default=str)
    print(f"=== {label} ({path}) ===")
    print("probe_version:", out["probe_version"])
    print("verdict      :", out["verdict"])
    env_free = {k: v for k, v in out["per_section"].items() if k != "identity"}
    env_verdict = "PASS" if all(v == "PASS" or v == "NOT_APPLICABLE" for v in env_free.values()) else (
        "FAIL" if any(v == "FAIL" for v in env_free.values()) else "UNKNOWN")
    print("per_section  :", json.dumps(out["per_section"], ensure_ascii=False))
    print("identity     : FAIL/unknown here is environmental (local build has no SSL);",
          "criteria-verdict excluding identity:", env_verdict)
    print("3-4 rows     :", json.dumps(out["sections"]["3-4"], ensure_ascii=False, default=str))
    print("3-7 rows     :", json.dumps(out["sections"]["3-7"], ensure_ascii=False, default=str))
    print("3-4b rows    :", json.dumps(out["sections"]["3-4b"], ensure_ascii=False, default=str))
    print("1D row count :", len(out["sections"]["1D"]))
    print("1A/1B/1C/1E  :", len(out["sections"]["1A"]), len(out["sections"]["1B"]),
          len(out["sections"]["1C"]), len(out["sections"]["1E"]))
    print("tro_after    :", json.dumps(out["sections"]["transaction_read_only_after"],
                                      ensure_ascii=False, default=str))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
