"""Story 10.1 migration 20261112_01: upgrades cleanly, downgrades cleanly, and is retry-safe."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.meeting.models import Base

BEFORE = "20261109_02"
NEW_TABLES = {
    "auth_login_sessions",
    "auth_refresh_tokens",
    "auth_identities",
    "oauth_login_states",
    "auth_audit_events",
}


def _config(database_path: Path) -> Config:
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def _inspect(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    return engine, inspect(engine)


def test_upgrade_adds_the_session_tables_and_the_access_token_link(tmp_path: Path):
    path = tmp_path / "up.sqlite"
    command.upgrade(_config(path), "20261112_01")

    engine, inspector = _inspect(path)
    try:
        assert NEW_TABLES <= set(inspector.get_table_names())
        assert "login_session_id" in {c["name"] for c in inspector.get_columns("auth_sessions")}
        # Only a hash of a refresh token can be stored, and each hash is unique.
        refresh = inspector.get_columns("auth_refresh_tokens")
        assert "token_hash" in {c["name"] for c in refresh} and "token" not in {c["name"] for c in refresh}
        assert any(u["column_names"] == ["token_hash"] for u in inspector.get_unique_constraints("auth_refresh_tokens"))
        # One identity per provider subject, and one per provider per account.
        uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("auth_identities")}
        assert {("provider", "subject"), ("account_id", "provider")} <= uniques
        assert any(u["column_names"] == ["state_hash"] for u in inspector.get_unique_constraints("oauth_login_states"))
    finally:
        engine.dispose()


def test_downgrade_removes_everything_and_the_upgrade_can_run_again(tmp_path: Path):
    path = tmp_path / "down.sqlite"
    config = _config(path)
    command.upgrade(config, "20261112_01")

    command.downgrade(config, BEFORE)
    engine, inspector = _inspect(path)
    try:
        assert not NEW_TABLES & set(inspector.get_table_names())
        assert "login_session_id" not in {c["name"] for c in inspector.get_columns("auth_sessions")}
        assert "auth_sessions" in inspector.get_table_names()   # the earlier table is untouched
    finally:
        engine.dispose()

    command.upgrade(config, "20261112_01")
    engine, inspector = _inspect(path)
    try:
        assert NEW_TABLES <= set(inspector.get_table_names())
    finally:
        engine.dispose()


def test_upgrade_is_safe_when_the_api_already_created_the_new_tables(tmp_path: Path):
    """The API creates missing tables at start-up; the migration must not trip over them."""
    path = tmp_path / "stale.sqlite"
    config = _config(path)
    command.upgrade(config, BEFORE)

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        Base.metadata.create_all(bind=engine)   # what the API does when it starts on the old schema
        assert NEW_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "20261112_01")      # must not raise "table already exists"

    engine, inspector = _inspect(path)
    try:
        assert "login_session_id" in {c["name"] for c in inspector.get_columns("auth_sessions")}
    finally:
        engine.dispose()
