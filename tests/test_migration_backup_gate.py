"""The migration gate refuses to migrate unless this run's verified dump exists.

Every external command is stubbed, so these tests exercise the shell logic only: no
PostgreSQL, Docker, rclone or object store is contacted.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate_with_backup.sh"


def _write(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    current: str = "a7c1d2e3f4b5 (head)",
    head: str = "c3d4e5f6a7b8",
    current_ok: bool = True,
    backup_ok: bool = True,
    backup_name: str | None = None,
    local_bytes: int = 4096,
    remote_bytes: int = 4096,
    env_extra: dict[str, str] | None = None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _write(
        bin_dir / "alembic",
        f'''echo "alembic $*" >> "{log}"
case "$1" in
  current)
    if [ {1 if current_ok else 0} -eq 0 ]; then echo "connection refused" >&2; exit 3; fi
    printf '%b\\n' "{current}"
    ;;
  heads) printf '%b\\n' "{head}" ;;
  upgrade) ;;
esac
''',
    )

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    name = backup_name if backup_name is not None else f"newsbot-backup-{today}-010101.dump"
    _write(
        bin_dir / "rclone",
        f'''echo "rclone $*" >> "{log}"
case "$1" in
  size) echo '{{"count":1,"bytes":{remote_bytes}}}' ;;
  lsf) echo "{name}" ;;
esac
''',
    )

    backup = tmp_path / "backup.sh"
    failure = "" if backup_ok else "echo 'pg_dump exploded' >&2\nexit 1\n"
    _write(
        backup,
        f'echo "backup" >> "{log}"\n{failure}'
        f'echo "BACKUP_FILENAME={name}"\n'
        f'echo "BACKUP_SIZE={local_bytes}"\n',
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_SCRIPT": str(backup),
        "BACKUP_REMOTE": "b2",
        "BACKUP_BUCKET": "newsbot-backups",
        "LOG": str(log),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_no_pending_migration_takes_no_backup(tmp_path):
    proc, calls = _run(tmp_path, current="c3d4e5f6a7b8 (head)", head="c3d4e5f6a7b8")

    assert proc.returncode == 0, proc.stderr
    assert "Schema is current" in proc.stdout
    assert "backup" not in calls
    assert not any("upgrade" in call for call in calls)


def test_a_failed_backup_blocks_the_migration(tmp_path):
    proc, calls = _run(tmp_path, backup_ok=False)

    assert proc.returncode != 0
    # The backup's own output is echoed (never swallowed) and the gate adds its reason.
    assert "pg_dump exploded" in proc.stdout
    assert "Backup failed" in proc.stderr
    assert "backup" in calls
    assert not any("alembic upgrade" in call for call in calls)


def test_the_exact_artifact_of_this_run_is_verified_then_migrated(tmp_path):
    proc, calls = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    upgrade_at = next(i for i, call in enumerate(calls) if "alembic upgrade" in call)
    assert calls.index("backup") < upgrade_at
    assert "Verified backup: newsbot-backup-" in proc.stdout


def test_a_newer_dump_from_another_day_cannot_satisfy_the_gate(tmp_path):
    proc, _calls = _run(tmp_path, backup_name="newsbot-backup-2020-01-01-010101.dump")

    assert proc.returncode != 0
    assert "does not belong to today" in proc.stderr


def test_an_unreported_artifact_blocks_the_migration(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _write(
        bin_dir / "alembic",
        f'echo "alembic $*" >> "{log}"\ncase "$1" in current) echo "a7c1d2e3f4b5";; heads) echo "c3d4e5f6a7b8";; esac\n',
    )
    _write(bin_dir / "rclone", f'echo "rclone $*" >> "{log}"\n')
    backup = tmp_path / "backup.sh"
    _write(backup, f'echo "backup" >> "{log}"\n')  # succeeds but reports nothing
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_SCRIPT": str(backup),
        "BACKUP_REMOTE": "b2",
        "BACKUP_BUCKET": "newsbot-backups",
        "LOG": str(log),
    }

    proc = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True
    )

    assert proc.returncode != 0
    assert "did not report the artifact" in proc.stderr
    assert "alembic upgrade" not in log.read_text(encoding="utf-8")


def test_a_size_mismatch_at_the_destination_blocks_the_migration(tmp_path):
    proc, _calls = _run(tmp_path, local_bytes=4096, remote_bytes=1024)

    assert proc.returncode != 0
    assert "bytes at the destination" in proc.stderr


def test_multiple_heads_block_the_migration(tmp_path):
    proc, calls = _run(tmp_path, head="c3d4e5f6a7b8\\nd4e5f6a7b8c9")

    assert proc.returncode != 0
    assert "Expected exactly one Alembic head" in proc.stderr
    assert "backup" not in calls


def test_multiple_current_revisions_block_the_migration(tmp_path):
    proc, calls = _run(tmp_path, current="a7c1d2e3f4b5 (head)\\nb8c9d0e1f2a3")

    assert proc.returncode != 0
    assert "current revisions" in proc.stderr
    assert "backup" not in calls


def test_a_database_without_a_revision_blocks_the_migration(tmp_path):
    proc, calls = _run(tmp_path, current="")

    assert proc.returncode != 0
    assert "no Alembic revision" in proc.stderr
    assert "backup" not in calls


def test_an_uninitialized_database_can_be_migrated_only_explicitly(tmp_path):
    proc, calls = _run(
        tmp_path, current="", env_extra={"ALLOW_UNINITIALIZED_DB": "1"}
    )

    assert proc.returncode == 0, proc.stderr
    assert "backup" in calls
    assert any("alembic upgrade" in call for call in calls)


def test_unrecognized_alembic_output_blocks_the_migration(tmp_path):
    proc, calls = _run(tmp_path, current="Warning: could not read alembic_version")

    assert proc.returncode != 0
    assert "Unrecognized Alembic current output" in proc.stderr
    assert "backup" not in calls


def test_a_failing_alembic_command_blocks_the_migration(tmp_path):
    proc, calls = _run(tmp_path, current_ok=False)

    assert proc.returncode != 0
    assert "alembic current failed" in proc.stderr
    assert "backup" not in calls
