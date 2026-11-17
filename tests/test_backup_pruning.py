"""Backup creates and verifies; pruning is explicit, scoped and fail-closed.

`docker` and `rclone` are stubbed, so no container runs, no object store is contacted and
nothing can be uploaded or deleted by these tests.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"
PRUNE = ROOT / "scripts" / "prune_backups.sh"


def _write(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _stubs(tmp_path: Path, *, delete_exit: int = 0, old_dumps: str = "") -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    # docker: pg_dump writes a fake archive to stdout, pg_restore consumes stdin.
    _write(
        bin_dir / "docker",
        f'''echo "docker $*" >> "{log}"
case "$*" in
  *pg_dump*) printf 'FAKE-CUSTOM-FORMAT-DUMP' ;;
  *pg_restore*) cat > /dev/null ;;
esac
''',
    )

    # rclone: size must match the local file so backup.sh's verification passes.
    _write(
        bin_dir / "rclone",
        f'''echo "rclone $*" >> "{log}"
case "$1" in
  copy|delete) exit {delete_exit} ;;
  size)
    target="${{@: -1}}"
    local_bytes="$(wc -c < "/tmp/$(basename "$target")" | tr -d ' ')"
    printf '{{"count":1,"bytes":%s}}\\n' "$local_bytes"
    ;;
  lsf) printf '%s' "{old_dumps}" ;;
esac
''',
    )
    return bin_dir, log


def _env(bin_dir: Path, log: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/newsbot",
        "BACKUP_REMOTE": "b2",
        "BACKUP_BUCKET": "newsbot-backups",
        "LOG": str(log),
    }
    env.update(extra)
    return env


def _run(script: Path, env: dict[str, str], *args: str):
    return subprocess.run(
        ["bash", str(script), *args], cwd=ROOT, env=env, capture_output=True, text=True
    )


# ==============================================================================
# backup.sh never deletes
# ==============================================================================

def test_a_normal_backup_never_deletes_anything(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="newsbot-backup-2020-01-01-000000.dump\n")

    proc = _run(BACKUP, _env(bin_dir, log))

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8")
    assert "rclone copy" in calls
    assert "rclone size" in calls
    assert "rclone delete" not in calls
    assert "min-age" not in calls
    assert "Backup completed successfully" in proc.stdout


# ==============================================================================
# prune_backups.sh is dry-run until asked
# ==============================================================================

def test_pruning_without_yes_deletes_nothing(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="newsbot-backup-2020-01-01-000000.dump\n")
    env = _env(bin_dir, log)

    proc = _run(PRUNE, env)

    assert proc.returncode == 0, proc.stderr
    assert "Dry run" in proc.stdout
    assert "newsbot-backup-2020-01-01-000000.dump" in proc.stdout
    calls = log.read_text(encoding="utf-8")
    assert "rclone lsf" in calls
    assert "rclone delete" not in calls


def test_pruning_is_scoped_and_only_runs_with_yes(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="newsbot-backup-2020-01-01-000000.dump\n")
    env = _env(bin_dir, log)

    proc = _run(PRUNE, env, "--yes")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8")
    delete_line = next(line for line in calls.splitlines() if line.startswith("rclone delete"))
    assert "b2:newsbot-backups/db/" in delete_line
    assert "--include newsbot-backup-*.dump" in delete_line
    assert "--min-age 7d" in delete_line


def test_pruning_honours_a_custom_retention(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="newsbot-backup-2020-01-01-000000.dump\n")
    env = _env(bin_dir, log)

    proc = _run(PRUNE, env, "--yes", "--retention-days", "14")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8")
    assert "--min-age 14d" in calls


def test_pruning_with_nothing_old_does_not_delete(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="")
    env = _env(bin_dir, log)

    proc = _run(PRUNE, env, "--yes")

    assert proc.returncode == 0, proc.stderr
    assert "Nothing to prune" in proc.stdout
    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_an_empty_destination_aborts_before_any_rclone_call(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="newsbot-backup-2020-01-01-000000.dump\n")

    for overrides in ({"BACKUP_REMOTE": ""}, {"BACKUP_BUCKET": ""}):
        proc = _run(PRUNE, _env(bin_dir, log, **overrides), "--yes")
        assert proc.returncode != 0
        assert "is required" in proc.stderr

    assert not log.exists()  # no rclone stub was invoked at all


def test_a_malformed_destination_aborts_before_any_rclone_call(tmp_path):
    bin_dir, log = _stubs(tmp_path, old_dumps="newsbot-backup-2020-01-01-000000.dump\n")

    proc = _run(PRUNE, _env(bin_dir, log, BACKUP_BUCKET="bad bucket"), "--yes")

    assert proc.returncode != 0
    assert "invalid BACKUP_BUCKET" in proc.stderr
    assert not log.exists()  # no rclone stub was invoked at all


def test_a_failed_delete_is_reported_and_fails(tmp_path):
    bin_dir, log = _stubs(
        tmp_path, delete_exit=2, old_dumps="newsbot-backup-2020-01-01-000000.dump\n"
    )

    proc = _run(PRUNE, _env(bin_dir, log), "--yes")

    assert proc.returncode != 0
    assert "rclone delete failed" in proc.stderr
