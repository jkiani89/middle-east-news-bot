"""Unit tests for Alembic configuration and migrations."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_config_and_script_directory():
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    assert ini_path.exists()

    cfg = Config(str(ini_path))
    script = ScriptDirectory.from_config(cfg)

    # Verify revisions exist in script directory
    revisions = list(script.walk_revisions())
    rev_ids = [r.revision for r in revisions]

    # Snapshot: verify known migrations exist (update this list when adding migrations)
    assert len(revisions) >= 4
    assert "b7d910315d55" in rev_ids  # initial schema
    assert "ec4cd113bf5a" in rev_ids  # text_source
    assert "e24bf6e4d3eb" in rev_ids  # parent_event_id
    assert "f1a2b3c4d5e6" in rev_ids  # gemini_rotator_tables

    # Structural: no branch conflicts (single linear chain)
    heads = script.get_heads()
    assert len(heads) == 1, f"Multiple heads detected: {heads}"

    # Snapshot: current head (update when intentionally adding migrations)
    head_rev = script.get_current_head()
    assert head_rev == "f1a2b3c4d5e6"
