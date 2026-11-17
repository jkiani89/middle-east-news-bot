"""restore_drill.sh validates and restores into a throwaway database, never production.

`rclone` and `docker` are stubbed, so these tests download nothing, start no container and
cannot reach any database. They cover the fail-closed guards (filename, path traversal,
production database name, DATABASE_URL, non-local host), the restore/validation failures
and the guaranteed cleanup on both success and failure.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRILL = ROOT / "scripts" / "restore_drill.sh"

VALID = "newsbot-backup-2026-11-17-202309.dump"

TABLES = "\n".join(
    [
        "sources",
        "articles",
        "events",
        "article_events",
        "summaries",
        "daily_reports",
        "bulletin_slots",
        "telegraph_reports",
        "job_logs",
        "gemini_suspended_keys",
        "gemini_key_stats",
        "gemini_request_log",
    ]
)
COUNTS = "\n".join(
    f"{name}\t{count}"
    for name, count in [
        ("sources", 4),
        ("articles", 120),
        ("events", 30),
        ("article_events", 90),
        ("summaries", 25),
        ("daily_reports", 6),
        ("bulletin_slots", 3),
        ("telegraph_reports", 2),
        ("job_logs", 40),
        ("gemini_suspended_keys", 0),
        ("gemini_key_stats", 2),
        ("gemini_request_log", 15),
    ]
)


# ==============================================================================
# Harness
# ==============================================================================

def _write(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _stubs(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _write(
        bin_dir / "rclone",
        f'''echo "rclone $*" >> "{log}"
case "$1" in
  copyto)
    if [ "${{DOWNLOAD_EXIT:-0}}" -ne 0 ]; then
      echo "rclone: simulated download failure" >&2
      exit "$DOWNLOAD_EXIT"
    fi
    if [ "${{DOWNLOAD_EMPTY:-0}}" -eq 1 ]; then
      : > "$3"
    else
      printf 'FAKE-CUSTOM-FORMAT-DUMP' > "$3"
    fi
    ;;
  *) echo "unexpected rclone subcommand $1" >&2; exit 1 ;;
esac
''',
    )

    _write(
        bin_dir / "docker",
        f'''echo "docker $*" >> "{log}"
case "$*" in
  *"pg_restore --list"*)
    cat > /dev/null
    if [ "${{LIST_EXIT:-0}}" -eq 0 ]; then
      printf '1; 1259 1 TABLE public sources drill\\n2; 1259 1 TABLE public articles drill\\n'
    fi
    exit "${{LIST_EXIT:-0}}"
    ;;
  *"pg_restore"*)
    cat > /dev/null
    exit "${{RESTORE_EXIT:-0}}"
    ;;
  *"DROP DATABASE"*) exit 0 ;;
  *"CREATE DATABASE"*) exit 0 ;;
  *"SELECT 1"*) printf '1\\n' ;;
  *"information_schema.tables"*) printf '%s\\n' "${{TABLES_OUT:-}}" ;;
  *"alembic_version"*) printf '%s\\n' "${{ALEMBIC_OUT-c3d4e5f6a7b8}}" ;;
  *"UNION ALL"*) printf '%s\\n' "${{COUNTS_OUT:-}}" ;;
  *"pg_constraint"*) printf '%s\\n' "${{CONSTRAINTS_OUT:-f\\t7\\np\\t12}}" ;;
  *) exit 0 ;;
esac
''',
    )
    return bin_dir, log


def _env(tmp_path: Path, bin_dir: Path, log: Path, **extra: str) -> dict[str, str]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_REMOTE": "b2",
        "BACKUP_BUCKET": "newsbot-backups",
        "DRILL_PGHOST": "127.0.0.1",
        "DRILL_PGPORT": "5432",
        "DRILL_PGDATABASE": "newsbot_restore_check",
        "DRILL_PGUSER": "drill",
        "DRILL_PGPASSWORD": "drill_password",
        "TMPDIR": str(scratch),
        "LOG": str(log),
        "TABLES_OUT": TABLES,
        "COUNTS_OUT": COUNTS,
        "CONSTRAINTS_OUT": "f\t7\np\t12",
    }
    for name in ("DATABASE_URL", "PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "B2_BUCKET_NAME"):
        env.pop(name, None)
    env.update(extra)
    return env


def _run(env: dict[str, str], *args: str):
    return subprocess.run(
        ["bash", str(DRILL), *args], cwd=ROOT, env=env, capture_output=True, text=True
    )


def _calls(log: Path) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _assert_no_remote_writes(calls: str) -> None:
    for forbidden in ("rclone sync", "rclone delete", "rclone move", "rclone purge", "rclone copy "):
        assert forbidden not in calls, calls


# ==============================================================================
# Input validation
# ==============================================================================

def test_a_valid_dump_name_is_accepted(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log), VALID)

    assert proc.returncode == 0, proc.stderr
    assert "Restore drill PASSED" in proc.stdout


def test_invalid_filenames_are_rejected_before_any_remote_call(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(tmp_path, bin_dir, log)

    for name in (
        "backup.dump",
        "newsbot-backup-2026-11-17.dump",  # missing the time component
        "newsbot-backup-2026-11-17-202309.dump.exe",
        "newsbot-backup-2026-11-17-202309.dump.bak",
        "NEWSBOT-BACKUP-2026-11-17-202309.dump",
        "",
    ):
        proc = _run(env, name)
        assert proc.returncode != 0, name
        assert "invalid backup filename" in proc.stderr, name

    assert not log.exists()


def test_path_traversal_attempts_are_rejected(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(tmp_path, bin_dir, log)

    for name in (
        "../../etc/passwd",
        "db/newsbot-backup-2026-11-17-202309.dump",
        "/tmp/newsbot-backup-2026-11-17-202309.dump",
        "newsbot-backup-2026-11-17-202309.dump/../../x",
        "../newsbot-backup-2026-11-17-202309.dump",
    ):
        proc = _run(env, name)
        assert proc.returncode != 0, name
        assert "invalid backup filename" in proc.stderr, name

    assert not log.exists()


def test_a_non_b2_remote_is_rejected(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, BACKUP_REMOTE="r2"), VALID)

    assert proc.returncode != 0
    assert "only reads from b2" in proc.stderr
    assert not log.exists()


def test_an_invalid_bucket_name_is_rejected(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, BACKUP_BUCKET="bad bucket"), VALID)

    assert proc.returncode != 0
    assert "invalid bucket name" in proc.stderr
    assert not log.exists()


# ==============================================================================
# Production isolation
# ==============================================================================

def test_production_database_names_are_refused(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for name in ("newsbot", "postgres", "production", "newsbot_prod", "newsbot_restore_check2"):
        proc = _run(_env(tmp_path, bin_dir, log, DRILL_PGDATABASE=name), VALID)
        assert proc.returncode != 0, name
        assert "only newsbot_restore_check is allowed" in proc.stderr, name

    assert not log.exists()


def test_a_non_local_database_host_is_refused(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for key, host in (
        ("DRILL_PGHOST", "db.example.com"),
        ("DRILL_PGHOST", "10.0.0.5"),
        ("PGHOST", "prod-db.internal"),
    ):
        proc = _run(_env(tmp_path, bin_dir, log, **{key: host}), VALID)
        assert proc.returncode != 0, (key, host)
        assert "non-local database host" in proc.stderr, (key, host)

    assert not log.exists()


def test_a_present_database_url_stops_the_drill_immediately(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(
        _env(
            tmp_path,
            bin_dir,
            log,
            DATABASE_URL="postgresql+asyncpg://user:pass@prod.example.com:5432/newsbot",
        ),
        VALID,
    )

    assert proc.returncode != 0
    assert "DATABASE_URL is set" in proc.stderr
    assert not log.exists()


def test_localhost_and_127_0_0_1_are_both_accepted(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for host in ("127.0.0.1", "localhost"):
        proc = _run(_env(tmp_path, bin_dir, log, DRILL_PGHOST=host), VALID)
        assert proc.returncode == 0, (host, proc.stderr)


def test_the_drill_works_without_any_database_url(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(tmp_path, bin_dir, log)
    assert "DATABASE_URL" not in env

    proc = _run(env, VALID)

    assert proc.returncode == 0, proc.stderr
    assert "Restore drill PASSED" in proc.stdout


# ==============================================================================
# Download and restore behaviour
# ==============================================================================

def test_only_the_named_object_is_downloaded(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log), VALID)

    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    assert f"rclone copyto b2:newsbot-backups/db/{VALID}" in calls
    _assert_no_remote_writes(calls)


def test_a_failed_download_stops_with_a_readfiles_hint(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, DOWNLOAD_EXIT="1"), VALID)

    assert proc.returncode != 0
    assert "readFiles" in proc.stderr
    assert "pg_restore" not in _calls(log)


def test_an_empty_download_is_rejected(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, DOWNLOAD_EMPTY="1"), VALID)

    assert proc.returncode != 0
    assert "empty" in proc.stderr
    assert "pg_restore" not in _calls(log)


def test_an_unreadable_archive_fails_the_drill(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, LIST_EXIT="1"), VALID)

    assert proc.returncode != 0
    assert "pg_restore --list rejected" in proc.stderr
    assert "CREATE DATABASE" not in _calls(log)


def test_a_failed_restore_fails_the_drill(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, RESTORE_EXIT="1"), VALID)

    assert proc.returncode != 0
    assert "pg_restore failed" in proc.stderr
    assert "Restore drill PASSED" not in proc.stdout


def test_the_restore_uses_the_safe_flags(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log), VALID)

    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    restore = [line for line in calls.splitlines() if "pg_restore" in line and "--exit-on-error" in line]
    assert restore, calls
    for flag in ("--no-owner", "--no-privileges", "--exit-on-error"):
        assert flag in restore[0], restore[0]


# ==============================================================================
# Validation results
# ==============================================================================

def test_a_missing_table_fails_the_drill(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    incomplete = "\n".join(TABLES.splitlines()[:-1])

    proc = _run(_env(tmp_path, bin_dir, log, TABLES_OUT=incomplete), VALID)

    assert proc.returncode != 0
    assert "missing table(s)" in proc.stderr
    assert "gemini_request_log" in proc.stderr


def test_an_empty_alembic_version_fails_the_drill(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, ALEMBIC_OUT=""), VALID)

    assert proc.returncode != 0
    assert "alembic_version is empty" in proc.stderr


def test_no_foreign_keys_fails_the_drill(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log, CONSTRAINTS_OUT="p\t12"), VALID)

    assert proc.returncode != 0
    assert "no foreign keys" in proc.stderr


def test_row_counts_are_reported_without_contents(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(_env(tmp_path, bin_dir, log), VALID)

    assert proc.returncode == 0, proc.stderr
    assert "articles" in proc.stdout
    assert "120" in proc.stdout


# ==============================================================================
# Cleanup
# ==============================================================================

def test_cleanup_runs_after_success(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(tmp_path, bin_dir, log)

    proc = _run(env, VALID)

    assert proc.returncode == 0, proc.stderr
    assert "Cleanup" in proc.stdout
    assert "DROP DATABASE" in _calls(log)
    assert list(Path(env["TMPDIR"]).iterdir()) == []


def test_cleanup_runs_after_failure(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(tmp_path, bin_dir, log, RESTORE_EXIT="1")

    proc = _run(env, VALID)

    assert proc.returncode != 0
    assert "Cleanup" in proc.stdout
    assert "DROP DATABASE" in _calls(log)
    assert list(Path(env["TMPDIR"]).iterdir()) == []
