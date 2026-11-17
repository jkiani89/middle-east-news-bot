"""Deployment wiring: the Telegraph env, the backup gate, and the shared backup target.

Text assertions on purpose: PyYAML is not a project dependency, and these tests must run
without the GitHub Actions runtime.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_cron_passes_telegraph_settings_and_is_disabled_by_default():
    cron = _read(".github/workflows/cron.yml")

    assert "TELEGRAPH_ACCESS_TOKEN: ${{ secrets.TELEGRAPH_ACCESS_TOKEN }}" in cron
    assert "TELEGRAPH_ENABLED: ${{ vars.TELEGRAPH_ENABLED || 'false' }}" in cron
    assert "TELEGRAPH_AUTHOR_NAME:" in cron
    assert "TELEGRAPH_SHORT_NAME:" in cron


def test_cron_migrates_only_through_the_backup_gate():
    cron = _read(".github/workflows/cron.yml")

    assert "bash scripts/migrate_with_backup.sh" in cron
    assert "bash scripts/resolve_backup_storage.sh" in cron
    assert "run: alembic upgrade head" not in cron


def test_both_workflows_share_the_backup_target_resolution():
    assert "bash scripts/resolve_backup_storage.sh" in _read(".github/workflows/backup.yml")
    cron = _read(".github/workflows/cron.yml")
    for key in (
        "B2_APPLICATION_KEY_ID",
        "B2_APPLICATION_KEY",
        "B2_BUCKET_NAME",
        "BACKUP_STORAGE",
    ):
        assert key in cron


def test_deployment_docs_state_the_effective_grace_window():
    docs = _read("docs/DEPLOYMENT.md")

    assert "`SLOT_GRACE_HOURS` (default 6)" in docs
    assert "08:00-14:00 and 20:00-02:00 Tehran" in docs
