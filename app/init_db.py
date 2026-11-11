"""Database initialization script using Alembic migrations."""

from pathlib import Path

from alembic.config import Config

from alembic import command


def run_migrations() -> None:
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    command.upgrade(alembic_cfg, "head")
    print("Database migrations applied successfully to head.")


if __name__ == "__main__":
    run_migrations()
