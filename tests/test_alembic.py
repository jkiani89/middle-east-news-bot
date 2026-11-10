"""Unit tests for Alembic configuration and migrations."""

from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_config_and_script_directory():
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    assert ini_path.exists()
    
    cfg = Config(str(ini_path))
    script = ScriptDirectory.from_config(cfg)
    
    # Verify the initial revision exists
    revisions = list(script.walk_revisions())
    assert len(revisions) >= 1
    head_rev = script.get_current_head()
    assert head_rev == "b7d910315d55"
