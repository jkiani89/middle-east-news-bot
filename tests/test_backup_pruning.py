"""Backup creates and verifies; pruning is explicit, scoped, bounded and fail-closed.

`docker` and `rclone` are stubbed, so no container runs, no object store is contacted and
nothing can be uploaded or deleted by these tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"
PRUNE = ROOT / "scripts" / "prune_backups.sh"


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

    # rclone: copy/size support the backup test; lsjson/delete support the prune tests.
    _write(
        bin_dir / "rclone",
        f'''echo "rclone $*" >> "{log}"
case "$1" in
  copy) exit 0 ;;
  size)
    target="${{@: -1}}"
    printf '{{"count":1,"bytes":%s}}\\n' "$(wc -c < "/tmp/$(basename "$target")" | tr -d ' ')"
    ;;
  lsjson)
    if [ "${{LSJSON_EXIT:-0}}" -ne 0 ]; then
      echo "lsjson: simulated failure" >&2
      exit "$LSJSON_EXIT"
    fi
    printf '%s' "${{LSJSON_OUT:-[]}}"
    ;;
  delete)
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--files-from-raw" ]; then
        cp "$arg" "{tmp_path}/deleted.list"
      fi
      prev="$arg"
    done
    exit "${{DELETE_EXIT:-0}}"
    ;;
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
    env.pop("BACKUP_RETENTION_DAYS", None)
    env.pop("BACKUP_MIN_KEEP", None)
    env.update(extra)
    return env


def _run(script: Path, env: dict[str, str], *args: str):
    return subprocess.run(
        ["bash", str(script), *args], cwd=ROOT, env=env, capture_output=True, text=True
    )


def _modtime(days_ago: float) -> str:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entries(*entries: tuple[float, int, str]) -> list[dict]:
    return [
        {"Path": name, "Size": size, "ModTime": _modtime(age)}
        for age, size, name in entries
    ]


def _lsjson(*entries: tuple[float, int, str]) -> str:
    return json.dumps(_entries(*entries))


def _deleted_list(tmp_path: Path) -> list[str]:
    path = tmp_path / "deleted.list"
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _previewed_names(stdout: str) -> list[str]:
    lines = stdout.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("Eligible for deletion:"))
    except StopIteration:
        return []
    names = []
    for line in lines[start + 1 :]:
        if not line.startswith("  ") or not line.strip():
            break
        names.append(line.strip())
    return names


# ==============================================================================
# backup.sh never deletes
# ==============================================================================

def test_a_normal_backup_never_deletes_anything(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(BACKUP, _env(bin_dir, log))

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8")
    assert "rclone copy" in calls
    assert "rclone size" in calls
    assert "rclone delete" not in calls
    assert "min-age" not in calls
    assert "Backup completed successfully" in proc.stdout


# ==============================================================================
# Pruning is preview-first
# ==============================================================================

def test_pruning_without_yes_deletes_nothing(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(
        bin_dir,
        log,
        LSJSON_OUT=_lsjson(
            (1, 100, "newsbot-backup-2026-11-16-000000.dump"),
            (9, 200, "newsbot-backup-2026-11-08-000000.dump"),
            (10, 300, "newsbot-backup-2026-11-07-000000.dump"),
            (11, 400, "newsbot-backup-2026-11-06-000000.dump"),
            (12, 500, "newsbot-backup-2026-11-05-000000.dump"),
        ),
    )

    proc = _run(PRUNE, env)

    assert proc.returncode == 0, proc.stderr
    assert "Dry run" in proc.stdout
    # Newest three (1, 9 and 10 days old) are kept; only 11 and 12 days old qualify.
    assert _previewed_names(proc.stdout) == [
        "newsbot-backup-2026-11-06-000000.dump",
        "newsbot-backup-2026-11-05-000000.dump",
    ]
    calls = log.read_text(encoding="utf-8")
    assert "rclone lsjson" in calls
    assert "rclone delete" not in calls


# ==============================================================================
# Retention floor: newest three are always kept
# ==============================================================================

def test_the_newest_three_backups_are_always_kept(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    entries = (
        (9, 100, "newsbot-backup-2026-11-08-000000.dump"),
        (10, 200, "newsbot-backup-2026-11-07-000000.dump"),
        (11, 300, "newsbot-backup-2026-11-06-000000.dump"),
        (12, 400, "newsbot-backup-2026-11-05-000000.dump"),
        (13, 500, "newsbot-backup-2026-11-04-000000.dump"),
        (14, 600, "newsbot-backup-2026-11-03-000000.dump"),
    )

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=_lsjson(*entries)), "--yes")

    assert proc.returncode == 0, proc.stderr
    assert _deleted_list(tmp_path) == [
        "newsbot-backup-2026-11-05-000000.dump",
        "newsbot-backup-2026-11-04-000000.dump",
        "newsbot-backup-2026-11-03-000000.dump",
    ]


def test_nothing_is_deleted_when_only_three_backups_exist(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    entries = (
        (30, 100, "newsbot-backup-2026-10-18-000000.dump"),
        (31, 200, "newsbot-backup-2026-10-17-000000.dump"),
        (32, 300, "newsbot-backup-2026-10-16-000000.dump"),
    )

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=_lsjson(*entries)), "--yes")

    assert proc.returncode == 0, proc.stderr
    assert "Nothing to prune" in proc.stdout
    assert _deleted_list(tmp_path) == []
    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_the_newest_three_survive_even_when_all_backups_are_old(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    entries = (
        (40, 100, "newsbot-backup-2026-10-08-000000.dump"),
        (41, 200, "newsbot-backup-2026-10-07-000000.dump"),
        (42, 300, "newsbot-backup-2026-10-06-000000.dump"),
        (43, 400, "newsbot-backup-2026-10-05-000000.dump"),
        (44, 500, "newsbot-backup-2026-10-04-000000.dump"),
    )

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=_lsjson(*entries)), "--yes")

    assert proc.returncode == 0, proc.stderr
    assert _deleted_list(tmp_path) == [
        "newsbot-backup-2026-10-05-000000.dump",
        "newsbot-backup-2026-10-04-000000.dump",
    ]


def test_only_backups_older_than_eight_days_are_deleted(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    entries = (
        (1, 100, "newsbot-backup-2026-11-16-000000.dump"),
        (2, 200, "newsbot-backup-2026-11-15-000000.dump"),
        (3, 300, "newsbot-backup-2026-11-14-000000.dump"),
        (7.9, 400, "newsbot-backup-2026-11-09-000000.dump"),
        (8.5, 500, "newsbot-backup-2026-11-08-000000.dump"),
        (20, 600, "newsbot-backup-2026-10-28-000000.dump"),
    )

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=_lsjson(*entries)), "--yes")

    assert proc.returncode == 0, proc.stderr
    deleted = _deleted_list(tmp_path)
    assert "newsbot-backup-2026-11-09-000000.dump" not in deleted  # younger than 8d
    assert deleted == [
        "newsbot-backup-2026-11-08-000000.dump",
        "newsbot-backup-2026-10-28-000000.dump",
    ]


def test_preview_and_delete_act_on_exactly_the_same_set(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    env = _env(
        bin_dir,
        log,
        LSJSON_OUT=_lsjson(
            (2, 100, "newsbot-backup-2026-11-15-000000.dump"),
            (9, 200, "newsbot-backup-2026-11-08-000000.dump"),
            (10, 300, "newsbot-backup-2026-11-07-000000.dump"),
            (11, 400, "newsbot-backup-2026-11-06-000000.dump"),
            (12, 500, "newsbot-backup-2026-11-05-000000.dump"),
        ),
    )

    preview = _run(PRUNE, env)
    assert preview.returncode == 0, preview.stderr
    previewed = _previewed_names(preview.stdout)
    assert previewed

    applied = _run(PRUNE, env, "--yes")
    assert applied.returncode == 0, applied.stderr
    assert _deleted_list(tmp_path) == previewed


# ==============================================================================
# Fail-closed behaviour
# ==============================================================================

def test_a_failed_listing_blocks_deletion(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_EXIT="2"), "--yes")

    assert proc.returncode != 0
    assert "could not list" in proc.stderr
    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_an_unknown_modtime_aborts_before_deletion(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    # rclone reports unknown times as its --default-time (2000-01-01) and older builds as -1.
    for modtime in ("2000-01-01T00:00:00Z", "-1"):
        payload = json.dumps(
            [{"Path": "newsbot-backup-2026-11-01-000000.dump", "Size": 100, "ModTime": modtime}]
        )
        proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=payload), "--yes")
        assert proc.returncode != 0, modtime
        assert "ModTime" in proc.stderr, modtime

    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_a_future_modtime_aborts_before_deletion(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(
        [{"Path": "newsbot-backup-2026-11-01-000000.dump", "Size": 100, "ModTime": future}]
    )

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=payload), "--yes")

    assert proc.returncode != 0
    assert "future" in proc.stderr
    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_a_name_with_a_newline_cannot_inject_a_record(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    # A single object whose name embeds a newline and a whole fake record.
    injected = (
        "newsbot-backup-2026-11-01-000000.dump\n"
        "2026-11-17 20:00:00\t100\tnewsbot-backup-2026-11-02-000000.dump"
    )
    payload = json.dumps([{"Path": injected, "Size": 100, "ModTime": _modtime(30)}])

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=payload), "--yes")

    assert proc.returncode != 0
    assert "control character" in proc.stderr
    assert "rclone delete" not in log.read_text(encoding="utf-8")
    assert _deleted_list(tmp_path) == []


def test_a_name_with_a_control_character_is_rejected(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    payload = json.dumps(
        [{"Path": "newsbot-backup-2026-11-01-000000.dump\t", "Size": 100, "ModTime": _modtime(30)}]
    )

    proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=payload), "--yes")

    assert proc.returncode != 0
    assert "control character" in proc.stderr
    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_nested_and_unrelated_names_are_out_of_scope(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for name in (
        "archive/newsbot-backup-2026-11-01-000000.dump",
        "newsbot-backup-2026-11-01-000000.dump.exe",
        "other-backup-2026-11-01-000000.dump",
        "newsbot-backup-2026-11-01.dump",
    ):
        payload = json.dumps([{"Path": name, "Size": 100, "ModTime": _modtime(30)}])
        proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=payload), "--yes")
        assert proc.returncode != 0, name
        assert "not a backup of this project" in proc.stderr, name

    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_an_empty_destination_aborts_before_any_rclone_call(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for overrides in ({"BACKUP_REMOTE": ""}, {"BACKUP_BUCKET": ""}):
        proc = _run(PRUNE, _env(bin_dir, log, **overrides), "--yes")
        assert proc.returncode != 0
        assert "is required" in proc.stderr

    assert not log.exists()  # no rclone stub was invoked at all


def test_a_malformed_destination_aborts_before_any_rclone_call(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(PRUNE, _env(bin_dir, log, BACKUP_BUCKET="bad bucket"), "--yes")

    assert proc.returncode != 0
    assert "invalid BACKUP_BUCKET" in proc.stderr
    assert not log.exists()


def test_an_empty_listing_is_reported_and_never_deletes(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for payload in ("[]", ""):
        proc = _run(PRUNE, _env(bin_dir, log, LSJSON_OUT=payload), "--yes")
        assert proc.returncode != 0, payload
        assert "listing" in proc.stderr.lower(), payload

    assert "rclone delete" not in log.read_text(encoding="utf-8")


def test_invalid_retention_aborts_before_any_rclone_call(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    for args in (
        ("--retention-days", "0"),
        ("--retention-days", "-1"),
        ("--retention-days", "abc"),
        ("--retention-days",),
    ):
        proc = _run(PRUNE, _env(bin_dir, log), "--yes", *args)
        assert proc.returncode != 0, args
        assert "retention" in proc.stderr.lower(), args

    assert not log.exists()


def test_an_unknown_argument_does_not_enable_deletion(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(
        PRUNE,
        _env(bin_dir, log, LSJSON_OUT=_lsjson((10, 100, "newsbot-backup-2026-11-07-000000.dump"))),
        "--yes=1",
    )

    assert proc.returncode == 2
    assert "unknown argument" in proc.stderr
    assert not log.exists()


def test_the_temporary_files_are_removed_afterwards(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    proc = _run(
        PRUNE,
        _env(
            bin_dir,
            log,
            TMPDIR=str(scratch),
            LSJSON_OUT=_lsjson(
                (1, 100, "newsbot-backup-2026-11-16-000000.dump"),
                (9, 200, "newsbot-backup-2026-11-08-000000.dump"),
                (10, 300, "newsbot-backup-2026-11-07-000000.dump"),
                (11, 400, "newsbot-backup-2026-11-06-000000.dump"),
                (12, 500, "newsbot-backup-2026-11-05-000000.dump"),
            ),
        ),
        "--yes",
    )

    assert proc.returncode == 0, proc.stderr
    assert list(scratch.iterdir()) == []


def test_a_failed_delete_is_reported_and_fails(tmp_path):
    bin_dir, log = _stubs(tmp_path)

    proc = _run(
        PRUNE,
        _env(
            bin_dir,
            log,
            DELETE_EXIT="2",
            LSJSON_OUT=_lsjson(
                (1, 100, "newsbot-backup-2026-11-16-000000.dump"),
                (9, 200, "newsbot-backup-2026-11-08-000000.dump"),
                (10, 300, "newsbot-backup-2026-11-07-000000.dump"),
                (11, 400, "newsbot-backup-2026-11-06-000000.dump"),
                (12, 500, "newsbot-backup-2026-11-05-000000.dump"),
            ),
        ),
        "--yes",
    )

    assert proc.returncode != 0
    assert "rclone delete failed" in proc.stderr
