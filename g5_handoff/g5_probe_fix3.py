#!/usr/bin/env python3
"""G5 probe - read-only audit with audit_ro. Never prints the DSN."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import asyncpg

PROBE_VERSION = "fix3"
CRITERIA_NOTE = (
    "3-7 is scoped to non-system relations: pg_catalog, information_schema, "
    "pg_toast* and pg_temp* are excluded, so PostgreSQL's own predefined grants "
    "to PUBLIC are not attributed to the audited role. All other criteria are "
    "identical to fix2 (see CHANGES at end of file)."
)

QUERIES: dict[str, str] = {
    "identity": """
        SELECT current_user, session_user,
               (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl,
               inet_server_addr()::text AS server_addr, inet_server_port() AS server_port,
               current_database() AS db, current_setting('server_version') AS server_version,
               (SELECT count(*) FROM pg_event_trigger) AS event_trigger_visible_rows
        FROM pg_roles r WHERE r.rolname = current_user
    """,
    "event_trigger_visibility": """
        SELECT has_table_privilege(current_user, 'pg_catalog.pg_event_trigger', 'SELECT') AS can_read
    """,
    "transaction_read_only": """
        SELECT current_setting('transaction_read_only') AS tro,
               pg_current_xact_id_if_assigned()::text AS xid_assigned
    """,
    "1A": """
        WITH RECURSIVE inh(member, roleid, depth, path, chain_inherits) AS (
          SELECT m.member, m.roleid, 1, ARRAY[m.roleid], m.inherit_option
          FROM pg_auth_members m
          WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
          UNION ALL
          SELECT m.member, m.roleid, i.depth + 1, i.path || m.roleid,
                 i.chain_inherits AND m.inherit_option
          FROM pg_auth_members m JOIN inh i ON m.member = i.roleid
          WHERE NOT m.roleid = ANY(i.path)
        )
        SELECT r.rolname, min(i.depth) AS distance,
               bool_or(i.chain_inherits) AS inherited_automatically
        FROM inh i JOIN pg_roles r ON r.oid = i.roleid
        GROUP BY r.rolname ORDER BY 3 DESC, 2
    """,
    "1B": """
        WITH RECURSIVE sr(member, roleid, depth, path, chain_set) AS (
          SELECT m.member, m.roleid, 1, ARRAY[m.roleid], m.set_option
          FROM pg_auth_members m
          WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
          UNION ALL
          SELECT m.member, m.roleid, s.depth + 1, s.path || m.roleid,
                 s.chain_set AND m.set_option
          FROM pg_auth_members m JOIN sr s ON m.member = s.roleid
          WHERE NOT m.roleid = ANY(s.path)
        )
        SELECT r.rolname, min(s.depth) AS distance,
               bool_or(s.chain_set) AS reachable_by_set_role,
               r.rolsuper, r.rolcreaterole, r.rolbypassrls
        FROM sr s JOIN pg_roles r ON r.oid = s.roleid
        GROUP BY 1,4,5,6 ORDER BY 3 DESC, 2
    """,
    "1C": """
        SELECT r.rolname,
          pg_has_role(current_user, r.oid, 'MEMBER') AS is_member,
          pg_has_role(current_user, r.oid, 'USAGE')  AS inherits_privileges,
          pg_has_role(current_user, r.oid, 'SET')    AS can_set_role
        FROM pg_roles r
        WHERE r.rolname <> current_user
          AND (pg_has_role(current_user, r.oid, 'MEMBER')
            OR pg_has_role(current_user, r.oid, 'USAGE')
            OR pg_has_role(current_user, r.oid, 'SET'))
        ORDER BY 4 DESC, 3 DESC, 2 DESC
    """,
    "1D": """
        WITH roles_in_scope(oid, rolname, via_inherit, via_set_role) AS (
          SELECT oid, rolname, false, false FROM pg_roles WHERE rolname = current_user
          UNION ALL
          SELECT r.oid, r.rolname,
                 pg_has_role(current_user, r.oid, 'USAGE'),
                 pg_has_role(current_user, r.oid, 'SET')
          FROM pg_roles r WHERE r.rolname <> current_user
            AND (pg_has_role(current_user, r.oid, 'USAGE')
              OR pg_has_role(current_user, r.oid, 'SET'))
        ),
        matrix AS (
          SELECT s.rolname, s.via_inherit, s.via_set_role, c.relkind,
                 n.nspname AS schema_name, c.relname,
            has_table_privilege(s.oid, c.oid, 'SELECT')     AS sel,
            has_table_privilege(s.oid, c.oid, 'INSERT')     AS ins,
            has_table_privilege(s.oid, c.oid, 'UPDATE')     AS upd,
            has_table_privilege(s.oid, c.oid, 'DELETE')     AS del,
            has_table_privilege(s.oid, c.oid, 'TRUNCATE')   AS trunc,
            has_table_privilege(s.oid, c.oid, 'REFERENCES') AS refs,
            has_table_privilege(s.oid, c.oid, 'TRIGGER')    AS trg,
            has_any_column_privilege(s.oid, c.oid, 'INSERT')     AS any_col_ins,
            has_any_column_privilege(s.oid, c.oid, 'UPDATE')     AS any_col_upd,
            has_any_column_privilege(s.oid, c.oid, 'REFERENCES') AS any_col_ref,
            (has_table_privilege(s.oid, c.oid, 'INSERT')
             OR has_table_privilege(s.oid, c.oid, 'UPDATE')
             OR has_table_privilege(s.oid, c.oid, 'DELETE')
             OR has_table_privilege(s.oid, c.oid, 'TRUNCATE')
             OR has_table_privilege(s.oid, c.oid, 'REFERENCES')
             OR has_table_privilege(s.oid, c.oid, 'TRIGGER')
             OR has_any_column_privilege(s.oid, c.oid, 'INSERT')
             OR has_any_column_privilege(s.oid, c.oid, 'UPDATE')
             OR has_any_column_privilege(s.oid, c.oid, 'REFERENCES')) AS any_write
          FROM roles_in_scope s
          CROSS JOIN pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE c.relkind IN ('r','p','v','m','f')
            AND n.oid NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
        )
        SELECT * FROM matrix ORDER BY any_write DESC, rolname, schema_name, relname
    """,
    "1E": """
        WITH roles_in_scope(oid, rolname, via_inherit, via_set_role) AS (
          SELECT oid, rolname, false, false FROM pg_roles WHERE rolname = current_user
          UNION ALL
          SELECT r.oid, r.rolname,
                 pg_has_role(current_user, r.oid, 'USAGE'),
                 pg_has_role(current_user, r.oid, 'SET')
          FROM pg_roles r WHERE r.rolname <> current_user
            AND (pg_has_role(current_user, r.oid, 'USAGE')
              OR pg_has_role(current_user, r.oid, 'SET'))
        )
        SELECT 'database' AS kind, d.datname AS object, d.datdba::regrole AS owner,
               s.via_inherit, s.via_set_role
        FROM pg_database d JOIN roles_in_scope s ON s.oid = d.datdba
        WHERE d.datname = current_database()
        UNION ALL
        SELECT 'tablespace', t.spcname, t.spcowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_tablespace t JOIN roles_in_scope s ON s.oid = t.spcowner
        UNION ALL
        SELECT 'schema', n.nspname, n.nspowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_namespace n JOIN roles_in_scope s ON s.oid = n.nspowner
        UNION ALL
        SELECT 'relation', n.nspname || '.' || c.relname, c.relowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN roles_in_scope s ON s.oid = c.relowner
        WHERE c.relkind IN ('r','p','v','m','f','S','i','I','c','t')
        UNION ALL
        SELECT 'type', nt.nspname || '.' || t.typname, t.typowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_type t JOIN pg_namespace nt ON nt.oid = t.typnamespace
        JOIN roles_in_scope s ON s.oid = t.typowner
        WHERE t.typtype IN ('b','c','d','e','r','m')
        UNION ALL
        SELECT 'language', l.lanname, l.lanowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_language l JOIN roles_in_scope s ON s.oid = l.lanowner
        UNION ALL
        SELECT 'fdw', f.fdwname, f.fdwowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_foreign_data_wrapper f JOIN roles_in_scope s ON s.oid = f.fdwowner
        UNION ALL
        SELECT 'foreign_server', v.srvname, v.srvowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_foreign_server v JOIN roles_in_scope s ON s.oid = v.srvowner
        UNION ALL
        SELECT 'extension', e.extname, e.extowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_extension e JOIN roles_in_scope s ON s.oid = e.extowner
        UNION ALL
        SELECT 'operator', o.oid::regoperator::text, o.oprowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_operator o JOIN roles_in_scope s ON s.oid = o.oprowner
        UNION ALL
        SELECT 'operator_class', no.nspname || '.' || oc.opcname || ' [' || am.amname || ']',
               oc.opcowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_opclass oc JOIN pg_namespace no ON no.oid = oc.opcnamespace
        JOIN pg_am am ON am.oid = oc.opcmethod
        JOIN roles_in_scope s ON s.oid = oc.opcowner
        UNION ALL
        SELECT 'operator_family', nof.nspname || '.' || of.opfname || ' [' || am.amname || ']',
               of.opfowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_opfamily of JOIN pg_namespace nof ON nof.oid = of.opfnamespace
        JOIN pg_am am ON am.oid = of.opfmethod
        JOIN roles_in_scope s ON s.oid = of.opfowner
        UNION ALL
        SELECT 'collation', nco.nspname || '.' || k.collname, k.collowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_collation k JOIN pg_namespace nco ON nco.oid = k.collnamespace
        JOIN roles_in_scope s ON s.oid = k.collowner
        UNION ALL
        SELECT 'conversion', ncv.nspname || '.' || cv.conname, cv.conowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_conversion cv JOIN pg_namespace ncv ON ncv.oid = cv.connamespace
        JOIN roles_in_scope s ON s.oid = cv.conowner
        UNION ALL
        SELECT 'text_search_config', ncfg.nspname || '.' || cfg.cfgname, cfg.cfgowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_ts_config cfg JOIN pg_namespace ncfg ON ncfg.oid = cfg.cfgnamespace
        JOIN roles_in_scope s ON s.oid = cfg.cfgowner
        UNION ALL
        SELECT 'text_search_dict', nd.nspname || '.' || dict.dictname, dict.dictowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_ts_dict dict JOIN pg_namespace nd ON nd.oid = dict.dictnamespace
        JOIN roles_in_scope s ON s.oid = dict.dictowner
        UNION ALL
        -- fix1/fix2: neither pg_ts_parser nor pg_ts_template tracks an owner column
        -- (text-search parsers/templates are not owned per object in PostgreSQL),
        -- so those two branches were removed. See CHANGES at EOF.
        SELECT 'statistics_ext', nst.nspname || '.' || st.stxname, st.stxowner::regrole,
               s.via_inherit, s.via_set_role
        FROM pg_statistic_ext st JOIN pg_namespace nst ON nst.oid = st.stxnamespace
        JOIN roles_in_scope s ON s.oid = st.stxowner
        UNION ALL
        SELECT 'large_object', m.oid::text, m.lomowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_largeobject_metadata m JOIN roles_in_scope s ON s.oid = m.lomowner
        UNION ALL
        SELECT 'event_trigger', et.evtname, et.evtowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_event_trigger et JOIN roles_in_scope s ON s.oid = et.evtowner
        UNION ALL
        SELECT 'routine_function',
               np.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               p.proowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_proc p JOIN pg_namespace np ON np.oid = p.pronamespace
        JOIN roles_in_scope s ON s.oid = p.proowner WHERE p.prokind = 'f'
        UNION ALL
        SELECT 'routine_procedure',
               np.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               p.proowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_proc p JOIN pg_namespace np ON np.oid = p.pronamespace
        JOIN roles_in_scope s ON s.oid = p.proowner WHERE p.prokind = 'p'
        UNION ALL
        SELECT 'routine_aggregate',
               np.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               p.proowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_proc p JOIN pg_namespace np ON np.oid = p.pronamespace
        JOIN roles_in_scope s ON s.oid = p.proowner WHERE p.prokind = 'a'
        UNION ALL
        SELECT 'routine_window',
               np.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               p.proowner::regrole, s.via_inherit, s.via_set_role
        FROM pg_proc p JOIN pg_namespace np ON np.oid = p.pronamespace
        JOIN roles_in_scope s ON s.oid = p.proowner WHERE p.prokind = 'w'
    """,
    "2": """
        WITH roles_in_scope(oid, rolname, via_inherit, via_set_role) AS (
          SELECT oid, rolname, false, false FROM pg_roles WHERE rolname = current_user
          UNION ALL
          SELECT r.oid, r.rolname,
                 pg_has_role(current_user, r.oid, 'USAGE'),
                 pg_has_role(current_user, r.oid, 'SET')
          FROM pg_roles r WHERE r.rolname <> current_user
            AND (pg_has_role(current_user, r.oid, 'USAGE')
              OR pg_has_role(current_user, r.oid, 'SET'))
        ),
        lo_base AS (
          SELECT m.oid AS lo_oid, m.lomowner AS lo_owner,
                 (m.lomacl IS NULL) AS used_default_acl,
                 a.grantee AS grantee_oid, a.privilege_type AS priv,
                 a.is_grantable AS is_grantable
          FROM pg_largeobject_metadata m
          CROSS JOIN LATERAL aclexplode(coalesce(m.lomacl, acldefault('L', m.lomowner))) AS a
        ),
        lo_paths AS (
          SELECT 'public'::text AS via, NULL::text AS rolname, b.lo_oid, b.priv
          FROM lo_base b WHERE b.grantee_oid = 0
          UNION ALL
          SELECT 'owner', s.rolname, m.oid, 'UPDATE'::text
          FROM pg_largeobject_metadata m JOIN roles_in_scope s ON s.oid = m.lomowner
          UNION ALL
          SELECT p.via, s.rolname, b.lo_oid, b.priv
          FROM lo_base b
          JOIN roles_in_scope s ON s.oid = b.grantee_oid
          CROSS JOIN LATERAL (VALUES
              ('self',     s.rolname = current_user),
              ('inherit',  s.via_inherit),
              ('set_role', s.via_set_role)) AS p(via, applies)
          WHERE b.grantee_oid <> 0 AND p.applies
        )
        SELECT
          (SELECT count(*) FROM pg_largeobject_metadata)           AS total_large_objects,
          (SELECT count(*) FROM lo_base)                           AS total_acl_entries,
          current_setting('lo_compat_privileges')                  AS lo_compat_privileges,
          count(*) FILTER (WHERE via='public'   AND priv='UPDATE') AS public_write_paths,
          count(*) FILTER (WHERE via='owner')                      AS owner_write_paths,
          count(*) FILTER (WHERE via='self'     AND priv='UPDATE') AS self_write_paths,
          count(*) FILTER (WHERE via='inherit'  AND priv='UPDATE') AS inherit_write_paths,
          count(*) FILTER (WHERE via='set_role' AND priv='UPDATE') AS set_role_write_paths,
          count(*) FILTER (WHERE priv='UPDATE')                    AS write_path_rows,
          count(DISTINCT lo_oid) FILTER (WHERE priv='UPDATE')      AS distinct_lo_with_write_path
        FROM lo_paths
    """,
    "3-4": """
        SELECT has_database_privilege(current_user, current_database(), 'CONNECT')   AS can_connect,
               has_database_privilege(current_user, current_database(), 'TEMPORARY') AS can_temp,
               has_database_privilege(current_user, current_database(), 'CREATE')    AS can_create_schema
    """,
    "3-4b": """
        SELECT n.nspname,
               has_schema_privilege(current_user, n.oid, 'USAGE')  AS usage,
               has_schema_privilege(current_user, n.oid, 'CREATE') AS can_create
        FROM pg_namespace n
        WHERE n.nspname !~ '^pg_(toast|temp)'
        ORDER BY 2 DESC, 3 DESC, 1
    """,
    "3-6": """
        WITH roles_in_scope(oid, rolname, via_inherit, via_set_role) AS (
          SELECT oid, rolname, false, false FROM pg_roles WHERE rolname = current_user
          UNION ALL
          SELECT r.oid, r.rolname,
                 pg_has_role(current_user, r.oid, 'USAGE'),
                 pg_has_role(current_user, r.oid, 'SET')
          FROM pg_roles r WHERE r.rolname <> current_user
            AND (pg_has_role(current_user, r.oid, 'USAGE')
              OR pg_has_role(current_user, r.oid, 'SET'))
        )
        SELECT s.rolname, n.nspname || '.' || c.relname AS seq,
               has_sequence_privilege(s.oid, c.oid, 'SELECT') AS sel,
               has_sequence_privilege(s.oid, c.oid, 'USAGE')  AS usage,
               has_sequence_privilege(s.oid, c.oid, 'UPDATE') AS upd
        FROM roles_in_scope s CROSS JOIN pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'S'
        ORDER BY (has_sequence_privilege(s.oid, c.oid, 'USAGE')
                  OR has_sequence_privilege(s.oid, c.oid, 'UPDATE')) DESC,
                 s.rolname, seq
    """,
    "3-7": """
        SELECT n.nspname || '.' || c.relname AS object, c.relkind,
               x.privilege_type, x.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) AS x
        WHERE x.grantee = 0 AND x.privilege_type <> 'SELECT'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp)'
        ORDER BY 1, 2
    """,
    "3-7b": """
        SELECT 'schema' AS kind, n.nspname AS object, x.privilege_type, x.is_grantable
        FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) AS x
        WHERE x.grantee = 0 AND x.privilege_type NOT IN ('USAGE')
        UNION ALL
        SELECT 'database', d.datname, x.privilege_type, x.is_grantable
        FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
        WHERE x.grantee = 0 AND x.privilege_type NOT IN ('CONNECT','TEMPORARY')
        UNION ALL
        SELECT 'routine', np.nspname || '.' || p.proname, x.privilege_type, x.is_grantable
        FROM pg_proc p JOIN pg_namespace np ON np.oid = p.pronamespace
        CROSS JOIN LATERAL aclexplode(p.proacl) AS x
        WHERE x.grantee = 0 AND x.privilege_type <> 'EXECUTE'
    """,
    "3-8": """
        WITH roles_in_scope(oid, rolname, via_inherit, via_set_role) AS (
          SELECT oid, rolname, false, false FROM pg_roles WHERE rolname = current_user
          UNION ALL
          SELECT r.oid, r.rolname,
                 pg_has_role(current_user, r.oid, 'USAGE'),
                 pg_has_role(current_user, r.oid, 'SET')
          FROM pg_roles r WHERE r.rolname <> current_user
            AND (pg_has_role(current_user, r.oid, 'USAGE')
              OR pg_has_role(current_user, r.oid, 'SET'))
        ),
        secdef AS (
          SELECT p.oid AS f_oid, p.pronamespace AS f_ns, n.nspname AS f_schema,
                 p.proname AS f_name, p.prokind AS f_kind, p.proowner AS f_owner
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
          WHERE p.prosecdef
        )
        SELECT p.f_schema, p.f_name, p.f_kind, p.f_owner::regrole AS owner,
               s.rolname AS executable_by, s.via_inherit, s.via_set_role
        FROM secdef p CROSS JOIN roles_in_scope s
        WHERE has_function_privilege(s.oid, p.f_oid, 'EXECUTE')
          AND p.f_ns NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
        ORDER BY s.via_set_role, s.via_inherit, p.f_schema, p.f_name
    """,
    "3-8-count": """
        SELECT count(*) AS executable_secdef_outside_system_schemas
        FROM pg_proc p CROSS JOIN (
          SELECT oid FROM pg_roles WHERE rolname = current_user
          UNION SELECT r.oid FROM pg_roles r
          WHERE pg_has_role(current_user, r.oid, 'USAGE')
             OR pg_has_role(current_user, r.oid, 'SET')
        ) s
        WHERE p.prosecdef
          AND p.pronamespace NOT IN ('pg_catalog'::regnamespace,
                                     'information_schema'::regnamespace)
          AND has_function_privilege(s.oid, p.oid, 'EXECUTE')
    """,
}

FORBIDDEN_TOKENS = re.compile(
    r"\b(insert|update|delete|truncate|alter|create|drop|grant|revoke|copy|"
    r"vacuum|analyze|reindex|commit|savepoint|release|set\s+role|call|do|"
    r"pg_terminate_backend|pg_cancel_backend|lo_import|lo_export|lo_unlink)\b",
    re.IGNORECASE,
)


def strip_literals(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def assert_safe(key: str) -> str:
    if key not in QUERIES:
        raise SystemExit(f"GUARD: unknown query key {key!r}")
    sql = QUERIES[key]
    body = strip_literals(sql)
    first = body.strip().split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise SystemExit(f"GUARD: {key} is not a SELECT/WITH statement")
    if ";" in body:
        raise SystemExit(f"GUARD: {key} contains a statement separator")
    hit = FORBIDDEN_TOKENS.search(body)
    if hit:
        raise SystemExit(f"GUARD: {key} contains forbidden token {hit.group(0)!r}")
    return sql


def redact(dsn: str) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    return f"{parts.scheme}://{parts.username or ''}:***@{host}:{parts.port or ''}{parts.path}"


def build_ssl_context(dsn: str) -> ssl.SSLContext:
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query))
    ca = os.environ.get("POSTGRES_SSLROOTCERT") or query.get("sslrootcert")
    ctx = ssl.create_default_context(cafile=ca if ca else None)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def connect_parts(dsn: str) -> dict:
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]
    parts = urlsplit(dsn)
    query = {k: v for k, v in parse_qsl(parts.query) if k not in ("sslmode", "sslrootcert")}
    return {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "user": parts.username,
        "password": parts.password,
        "database": (parts.path or "/").lstrip("/") or "defaultdb",
        "ssl": build_ssl_context(dsn),
        "server_settings": dict(query),
    }


def status_of(rows: list[dict], predicate) -> str:
    return "PASS" if not any(predicate(r) for r in rows) else "FAIL"


def evaluate(ev: dict) -> dict:
    out: dict[str, str] = {}
    ident = ev["identity"][0]
    ok_ident = (
        ident["current_user"] == "audit_ro"
        and ident["session_user"] == "audit_ro"
        and ident["ssl"] is True
        and ident["server_addr"] not in (None, "127.0.0.1", "::1")
    )
    out["identity"] = "PASS" if ok_ident else "FAIL"
    tro = ev["transaction_read_only"][0]
    out["read_only_txn"] = (
        "PASS" if str(tro["tro"]).lower() == "on" and tro["xid_assigned"] is None else "FAIL"
    )
    out["1A"] = status_of(ev["1A"], lambda r: r["inherited_automatically"] is True)
    out["1B"] = status_of(ev["1B"], lambda r: r["reachable_by_set_role"] is True)
    out["1C"] = status_of(
        ev["1C"], lambda r: r["inherits_privileges"] is True or r["can_set_role"] is True
    )
    out["1D"] = status_of(ev["1D"], lambda r: r["any_write"] is True)
    ev_vis = bool(ev["event_trigger_visibility"][0]["can_read"])
    if ev["1E"]:
        out["1E"] = "FAIL"
    elif not ev_vis:
        out["1E"] = "UNKNOWN"
    else:
        out["1E"] = "PASS"
    lo = ev["2"][0]
    if str(lo["lo_compat_privileges"]).lower() == "on":
        out["2"] = "UNKNOWN"
    elif int(lo["total_large_objects"]) == 0:
        out["2"] = "NOT_APPLICABLE"
    elif any(
        int(lo[k]) > 0
        for k in (
            "public_write_paths", "owner_write_paths", "self_write_paths",
            "inherit_write_paths", "set_role_write_paths",
            "write_path_rows", "distinct_lo_with_write_path",
        )
    ):
        out["2"] = "FAIL"
    else:
        out["2"] = "PASS"
    dbp = ev["3-4"][0]
    out["3-4"] = "PASS" if not dbp["can_create_schema"] and not dbp["can_temp"] else "FAIL"
    out["3-4b"] = status_of(ev["3-4b"], lambda r: r["can_create"] is True)
    out["3-6"] = status_of(ev["3-6"], lambda r: r["usage"] is True or r["upd"] is True)
    out["3-7"] = "PASS" if not ev["3-7"] else "FAIL"
    out["3-7b"] = "PASS" if not ev["3-7b"] else "FAIL"
    out["3-8"] = (
        "PASS"
        if int(ev["3-8-count"][0]["executable_secdef_outside_system_schemas"]) == 0
        else "FAIL"
    )
    return out


def overall(per_section: dict[str, str]) -> str:
    if any(v == "FAIL" for v in per_section.values()):
        return "FAIL"
    if any(v == "UNKNOWN" for v in per_section.values()):
        return "UNKNOWN"
    return "PASS"


async def main() -> int:
    ap = argparse.ArgumentParser(description="G5 read-only audit probe")
    ap.add_argument("--dsn-env", default="DATABASE_URL")
    ap.add_argument("--report", default="/tmp/p0_0/g5_report.json")
    ap.add_argument("--slot-date", default="2026-11-19")
    ap.add_argument("--slot-hour", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        for key in QUERIES:
            assert_safe(key)
        print(f"DRY-RUN OK: {len(QUERIES)} fixed queries validated; no connection made.")
        return 0

    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"FAIL: env var {args.dsn_env} is not set", file=sys.stderr)
        return 5

    report: dict = {
        "probe_version": PROBE_VERSION,
        "criteria_note": CRITERIA_NOTE,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dsn_redacted": redact(dsn),
        "slot": f"{args.slot_date} {args.slot_hour:02d}:00",
        "sections": {},
    }
    try:
        kwargs = connect_parts(dsn)
    except Exception as exc:
        print(f"FAIL: cannot build connection parameters ({type(exc).__name__})", file=sys.stderr)
        return 3

    conn = await asyncpg.connect(**kwargs)
    tx = None
    try:
        for key in ("identity", "event_trigger_visibility"):
            report["sections"][key] = [dict(r) for r in await conn.fetch(assert_safe(key))]
        ident = report["sections"]["identity"][0]
        if ident["current_user"] != "audit_ro" or ident["session_user"] != "audit_ro":
            report["per_section"] = {"identity": "FAIL"}
            report["verdict"] = "FAIL:IDENTITY"
            return emit(report, args.report, 2)
        if ident["ssl"] is not True:
            report["per_section"] = {"identity": "FAIL"}
            report["verdict"] = "FAIL:TLS_NOT_ACTIVE"
            return emit(report, args.report, 3)
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SET TRANSACTION READ ONLY")
        for key in ("transaction_read_only", "1A", "1B", "1C", "1D", "1E", "2",
                    "3-4", "3-4b", "3-6", "3-7", "3-7b", "3-8", "3-8-count"):
            report["sections"][key] = [dict(r) for r in await conn.fetch(assert_safe(key))]
        report["sections"]["transaction_read_only_after"] = [
            dict(r) for r in await conn.fetch(assert_safe("transaction_read_only"))
        ]
        report["per_section"] = evaluate(report["sections"])
        report["verdict"] = overall(report["per_section"])
        code = 0 if report["verdict"] == "PASS" else (4 if report["verdict"] == "UNKNOWN" else 1)
        return emit(report, args.report, code)
    except Exception as exc:
        report["verdict"] = f"ERROR:{type(exc).__name__}"
        report["error_message"] = str(exc)[:400]
        return emit(report, args.report, 1)
    finally:
        if tx is not None:
            try:
                await tx.rollback()
            except Exception:
                pass
        await conn.close()


def emit(report: dict, path: str, code: int) -> int:
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    print(payload)
    print(f"\n[g5] verdict={report.get('verdict')} report={target} exit={code}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# CHANGES relative to the audited baseline g5_probe.py
#   baseline sha256: 569eddc02575920725d4be0f80ad0ba8ca531509405677226206d882d1ac9e22
#
#   fix1  query "1E": removed the pg_ts_parser branch
#         (real run 2026-11-19 on PG 18.6: "column prs.prsowner does not exist").
#   fix2  query "1E": also removed the pg_ts_template branch
#         (real run fix1: "column tmpl.tmplowner does not exist").
#         Neither pg_ts_parser nor pg_ts_template has an owner column; text-search
#         parsers/templates are not owned per object, so no row is lost.
#   fix2  query "3-6": ORDER BY used the output aliases inside an expression
#         ("ORDER BY (usage OR upd)") which PostgreSQL rejects; the two
#         has_sequence_privilege() calls are now repeated. Ordering only -
#         the row set and therefore the verdict are unchanged.
#
#   Validation (fix2, offline): all 16 queries executed against a throwaway
#   local PostgreSQL 16.2 as a non-superuser role, inside BEGIN/SET TRANSACTION
#   READ ONLY/ROLLBACK; then the full fetch+evaluate()+overall() path was run via
#   asyncpg. No connection to Aiven, no project data.
#
#   Unchanged: guards, DSN/password handling, TLS verification, transaction
#   logic, every other query, and the row shape of all sections.

# ---------------------------------------------------------------------------
# CHANGES introduced by fix3 (relative to fix2, sha256 48af5bd3...f93939a)
#
#   fix3  query "3-7": criterion narrowed to NON-SYSTEM relations.
#         Real run (fix2, PG 18.6, 2026-11-19) returned exactly one row:
#           pg_catalog.pg_settings | v | UPDATE | is_grantable=false
#         That row is a predefined PostgreSQL grant, not role state:
#           src/backend/catalog/system_views.sql (REL_14 .. REL_18_6, master)
#           contains:  GRANT SELECT, UPDATE ON pg_settings TO PUBLIC;
#         It exists on every stock cluster, is identical for every role, and
#         therefore carries no information about audit_ro. It is also not
#         revocable here: system catalogs are owned by the bootstrap superuser,
#         while avnadmin and audit_ro are both rolsuper=f.
#         The UPDATE is mapped by rule pg_settings_u to
#         set_config(old.name, new.setting,'f'), and set_config still enforces
#         per-parameter privileges, so it grants no escalation; is_grantable is
#         false, so it cannot be passed on to another role.
#         => false positive. fix3 excludes pg_catalog, information_schema,
#            pg_toast* and pg_temp* from 3-7; every non-system relation is still
#            reported, so a real PUBLIC write grant cannot hide.
#
#   fix3  query "3-4b": filter bug fixed (latent, no result change).
#         fix1/fix2 contained  NOT LIKE 'pg\\_toast%'  inside a
#         standard-conforming string: the doubled backslash is a literal
#         backslash followed by the "_" wildcard, so the predicate matched
#         nothing and excluded nothing. Replaced with
#         nspname !~ '^pg_(toast|temp)'  which is what the audit intent
#         requires. Row set on the audited cluster is unchanged (public,
#         pg_catalog, information_schema only).
#
#   fix3  report metadata: "probe_version" and "criteria_note" are now written
#         into g5_report.json, so every report states which criteria produced it.
#
#   NOT changed by fix3 (deliberately):
#     - 3-4 is untouched. audit_ro really holds TEMPORARY through PUBLIC, so the
#       criterion must keep failing until an authorised human changes the
#       database ACL. Relaxing the PASS rule would mask a real finding.
#     - Every other query, guard, TLS setting, transaction handling and verdict
#       rule is byte-identical to fix2.
#
#   Validation (fix3, offline; details in docs/G5_AUDIT.md):
#     all 16 queries plus the full main() path were executed against a throwaway
#     local PostgreSQL as a non-superuser role, over TLS, inside a read-only
#     transaction; then repeated on that same throwaway cluster after
#     REVOKE TEMPORARY ON DATABASE ... FROM PUBLIC. No connection to Aiven and
#     no project data was touched.
