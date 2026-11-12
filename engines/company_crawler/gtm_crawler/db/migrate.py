"""Schema version helpers: readiness checks, migrating a database."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


@cache
def migration_head() -> str | None:
    """The newest migration revision shipped with this code."""
    return ScriptDirectory(str(MIGRATIONS)).get_current_head()


def upgrade_head(url: str) -> None:
    """``alembic upgrade head`` on the database at ``url``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@cache
def known_revisions() -> frozenset[str]:
    """Every migration revision shipped with this code (to tell a schema that is
    behind this code from one a newer release has already migrated)."""
    return frozenset(r.revision for r in ScriptDirectory(str(MIGRATIONS)).walk_revisions())
