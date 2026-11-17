"""backup.sh must not guess a destination outside GitHub Actions.

Inside Actions scripts/resolve_backup_storage.sh exports BACKUP_REMOTE/BACKUP_BUCKET and the
fallbacks stay as before. A local run without both variables stops before dumping or
uploading anything, so a development database can never land in the production bucket.

`docker` and `rclone` are stubbed: nothing is dumped and nothing is uploaded.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"


def _write(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _stubs(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _write(
        bin_dir / "docker",
        f'''echo "docker $*" >> "{log}"
case "$*" in
  *pg_dump*) printf 'FAKE-CUSTOM-FORMAT-DUMP' ;;
  *pg_restore*) cat > /dev/null ;;
esac
''',
    )
    _write(
        bin_dir / "rclone",
        f'''echo "rclone $*" >> "{log}"
case "$1" in
  copy) exit 0 ;;
  size)
    target="${{@: -1}}"
    printf '{{"count":1,"bytes":%s}}\\n' "$(wc -c < "/tmp/$(basename "$target")" | tr -d ' ')"
    ;;
esac
''',
    )
    return bin_dir, log


def _run(bin_dir: Path, log: Path, **extra: str):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/newsbot",
        "LOG": str(log),
    }
    for name in ("BACKUP_REMOTE", "BACKUP_BUCKET", "R2_BUCKET_NAME", "B2_BUCKET_NAME"):
        env.pop(name, None)
    env.pop("GITHUB_ACTIONS", None)
    env.update(extra)
    return subprocess.run(
        ["bash", str(BACKUP)], cwd=ROOT, env=env, capture_output=True, text=True
    )


def _called(log: Path) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_a_local_run_without_a_destination_stops_before_dumping(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(bin_dir, log)

    assert proc.returncode != 0
    assert "BACKUP_REMOTE and BACKUP_BUCKET" in proc.stderr
    assert _called(log) == ""


def test_a_local_run_with_only_one_destination_variable_stops(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for extra in (
        {"BACKUP_REMOTE": "b2"},
        {"BACKUP_BUCKET": "newsbot-backups"},
        {"BACKUP_REMOTE": "b2", "BACKUP_BUCKET": ""},
        {"BACKUP_REMOTE": "", "BACKUP_BUCKET": "newsbot-backups"},
    ):
        proc = _run(bin_dir, log, **extra)
        assert proc.returncode != 0, extra
        assert "BACKUP_REMOTE and BACKUP_BUCKET" in proc.stderr, extra

    assert _called(log) == ""


def test_a_local_run_with_both_variables_proceeds(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(bin_dir, log, BACKUP_REMOTE="b2", BACKUP_BUCKET="local-bucket")

    assert proc.returncode == 0, proc.stderr
    calls = _called(log)
    assert "rclone copy" in calls
    assert "b2:local-bucket/db/" in calls


def test_github_actions_keeps_the_previous_fallbacks(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(bin_dir, log, GITHUB_ACTIONS="true")

    assert proc.returncode == 0, proc.stderr
    calls = _called(log)
    assert "rclone copy" in calls
    assert "r2:newsbot-backups/db/" in calls
