"""Keep the database in step with the code, so the app never runs on an old schema.

Pulling new code used to leave the database one or more migrations behind, and the app
then failed in confusing ways (a missing table or column) until someone remembered to
run ``alembic upgrade head``. At startup the app now compares the database's revision
with the code's, and when it is behind it saves a backup and upgrades.

Only a *versioned* database is touched. One with no ``alembic_version`` table was built
straight from the models (a fresh install or a test database) and is left to
``create_tables``. Set ``AUTO_MIGRATE=false`` to turn this off.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

BACKEND_DIRECTORY = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIRECTORY / "alembic.ini"


class DatabaseMigrationError(RuntimeError):
    """The database could not be brought up to date; the app must not run on it."""


def auto_migrate_enabled() -> bool:
    explicit = os.environ.get("AUTO_MIGRATE", "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if explicit in ("1", "true", "yes", "on"):
        return True
    return os.environ.get("APP_ENV", "development").strip().lower() != "production"


def _config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_DIRECTORY / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(_config("sqlite://")).get_current_head()


def current_revision(engine: Engine) -> str | None:
    """The database's revision, or None when it has no version table (not versioned)."""
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _backup(database_path: Path, head: str) -> Path:
    target = database_path.with_name(f"{database_path.stem}_backup_before_{head}.db")
    source = sqlite3.connect(str(database_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)  # consistent even while the WAL has unsaved pages
        finally:
            destination.close()
    finally:
        source.close()
    return target


def ensure_database_current(engine: Engine) -> str:
    """Upgrade a versioned database that is behind the code.

    Returns what was done: ``"disabled"``, ``"unversioned"``, ``"current"`` or
    ``"upgraded"``. Raises :class:`DatabaseMigrationError` when it cannot be upgraded.
    """
    if not auto_migrate_enabled():
        return "disabled"
    current = current_revision(engine)
    if current is None:
        return "unversioned"
    head = head_revision()
    if current == head:
        return "current"

    scripts = ScriptDirectory.from_config(_config("sqlite://"))
    try:
        scripts.get_revision(current)
    except Exception as error:  # noqa: BLE001
        raise DatabaseMigrationError(
            f"The database is at revision {current}, which this version of the code does not "
            "know. It was probably used by a newer version; update the code (git pull) before "
            "starting the app."
        ) from error

    database_path = Path(engine.url.database) if engine.url.database else None
    backup = None
    if database_path is not None and database_path.exists():
        backup = _backup(database_path, head)
        logger.warning("Database backup saved at %s before upgrading %s -> %s", backup, current, head)
    logger.warning("Database is behind the code (%s, needs %s); upgrading now.", current, head)
    try:
        command.upgrade(_config(engine.url.render_as_string(hide_password=False)), "head")
    except Exception as error:  # noqa: BLE001
        raise DatabaseMigrationError(
            f"The database could not be upgraded from {current} to {head}: {error}. "
            + (f"Nothing was lost: a backup is at {backup}. " if backup else "")
            + "Fix the problem above, or run `alembic upgrade head` from the backend folder "
            "to see the full error."
        ) from error
    logger.warning("Database upgraded to %s.", head)
    return "upgraded"
