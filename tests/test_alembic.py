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
    assert len(revisions) >= 3
    rev_ids = [r.revision for r in revisions]
    assert "b7d910315d55" in rev_ids
    assert "ec4cd113bf5a" in rev_ids
    assert "e24bf6e4d3eb" in rev_ids
    head_rev = script.get_current_head()
    assert head_rev == "e24bf6e4d3eb"
