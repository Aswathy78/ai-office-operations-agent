import json
from pathlib import Path

import sqlite_vec
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db.migrate import head_revision
from app.meeting.models import Base


def test_full_migration_chain_upgrades_fresh_database_and_app_schema(tmp_path: Path):
    database_path = tmp_path / "fresh.sqlite"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == head_revision()
            assert connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'document_chunk_vectors'"
            ).scalar_one().startswith("CREATE VIRTUAL TABLE")

        Base.metadata.create_all(bind=engine)
        table_names = set(inspect(engine).get_table_names())
        assert {
            "meetings",
            "user_notifications",
            "workflow_runs",
            "workflow_steps",
            "workflow_dead_letters",
            "workflow_events",
        } <= table_names
    finally:
        engine.dispose()


def test_vector_migration_backfills_existing_chunk_source_and_embedding(
    tmp_path: Path,
):
    database_path = tmp_path / "existing_chunks.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        embedding = [0.25] + [0.0] * 511
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('20261103_02')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY, original_filename VARCHAR(512))"
            )
            connection.exec_driver_sql(
                "CREATE TABLE document_chunks (id VARCHAR(36) PRIMARY KEY, "
                "document_id VARCHAR(36) NOT NULL, embedding JSON NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO documents (id, original_filename) VALUES ('doc-1', 'policy.pdf')"
            )
            connection.exec_driver_sql(
                "INSERT INTO document_chunks (id, document_id, embedding) "
                "VALUES ('chunk-1', 'doc-1', ?)",
                (json.dumps(embedding),),
            )

        config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
        command.upgrade(config, "head")

        with engine.connect() as connection:
            raw_connection = connection.connection.driver_connection
            raw_connection.enable_load_extension(True)
            try:
                sqlite_vec.load(raw_connection)
            finally:
                raw_connection.enable_load_extension(False)
            assert connection.exec_driver_sql(
                "SELECT source FROM document_chunks WHERE id = 'chunk-1'"
            ).scalar_one() == "policy.pdf"
            vector = json.loads(
                connection.exec_driver_sql(
                    "SELECT vec_to_json(embedding) FROM document_chunk_vectors "
                    "WHERE chunk_id = 'chunk-1'"
                ).scalar_one()
            )
            assert vector[0] == 0.25
            assert len(vector) == 512
    finally:
        engine.dispose()


def test_migration_chain_handles_app_created_tables_with_a_stale_revision(
    tmp_path: Path,
):
    database_path = tmp_path / "existing_app_schema.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workflow_runs "
                "(id, workflow_type, idempotency_key, priority, status, payload, "
                "attempt_count, max_attempts, created_at, updated_at) "
                "VALUES ('run-1', 'test', 'key-1', 10, 'pending', '{}', 0, 3, "
                "'2026-11-04 00:00:00', '2026-11-04 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO workflow_steps "
                "(id, workflow_run_id, step_key, step_order, status, attempt_count, updated_at) "
                "VALUES ('step-1', 'run-1', 'test-step', 0, 'pending', 0, "
                "'2026-11-04 00:00:00')"
            )
            connection.exec_driver_sql("ALTER TABLE document_chunks DROP COLUMN source")
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('20261101_02')"
            )

        config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == "20261104_01"
            assert connection.exec_driver_sql(
                "SELECT id, status FROM workflow_runs WHERE id = 'run-1'"
            ).one() == ("run-1", "pending")
            assert connection.exec_driver_sql(
                "SELECT id, workflow_run_id FROM workflow_steps WHERE id = 'step-1'"
            ).one() == ("step-1", "run-1")
            assert "source" in {
                column["name"]
                for column in inspect(connection).get_columns("document_chunks")
            }
            assert connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'document_chunk_vectors'"
            ).first()
    finally:
        engine.dispose()
