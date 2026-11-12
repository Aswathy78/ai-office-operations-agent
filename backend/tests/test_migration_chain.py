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
            "assistant_runs",
            "assistant_run_steps",
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
            ).scalar_one() == head_revision()
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


def test_approval_center_migration_recovers_stale_batch_table_and_existing_link_table(
    tmp_path: Path,
):
    database_path = tmp_path / "approval_retry.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO assistant_action_approvals "
                "(id, tool_name, requester_id, requester_role, idempotency_key, arguments, "
                "arguments_hash, summary, risk_level, risk_reasons, approver_kind, status, "
                "expires_at, created_at, updated_at) "
                "VALUES ('approval-1', 'email.send', 'employee-1', 'employee', 'key-1', "
                "'{}', 'hash-1', 'Send an e-mail', 'high', '[]', 'approver', 'pending', "
                "'2026-11-09 00:00:00', '2026-11-08 00:00:00', '2026-11-08 00:00:00')"
            )
            approval_ddl = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'assistant_action_approvals'"
            ).scalar_one()
            connection.exec_driver_sql(
                approval_ddl.replace(
                    "CREATE TABLE assistant_action_approvals",
                    "CREATE TABLE _alembic_tmp_assistant_action_approvals",
                    1,
                )
            )
            connection.exec_driver_sql(
                "INSERT INTO _alembic_tmp_assistant_action_approvals "
                "SELECT * FROM assistant_action_approvals WHERE id = 'approval-1'"
            )
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('20261108_01')"
            )

        config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == head_revision()
            assert connection.exec_driver_sql(
                "SELECT id, status FROM assistant_action_approvals"
            ).one() == ("approval-1", "pending")
            assert {
                column["name"]
                for column in inspect(connection).get_columns("assistant_action_approvals")
            } >= {
                "alternate_approver_id",
                "reminder_sent_at",
                "expiry_notified_at",
            }
            assert connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name = '_alembic_tmp_assistant_action_approvals'"
            ).first() is None
            assert connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'assistant_approval_email_links'"
            ).first()
            assert {
                index["name"] for index in inspect(connection).get_indexes(
                    "assistant_approval_email_links"
                )
            } >= {"ix_assistant_approval_email_links_approval_recipient"}
    finally:
        engine.dispose()


def test_story_97_migration_is_retry_safe_and_preserves_approval_data(tmp_path: Path):
    database_path = tmp_path / "approval_lifecycle.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('20261108_02')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE assistant_action_approvals ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, "
                "status VARCHAR(20) NOT NULL, "
                "created_at DATETIME NOT NULL, "
                "expires_at DATETIME NOT NULL, "
                "alternate_approver_id VARCHAR(36), "
                "reminder_sent_at DATETIME)"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_action_approvals "
                "(id, status, created_at, expires_at, alternate_approver_id, reminder_sent_at) "
                "VALUES ('approval-preserved', 'pending', '2026-11-08 10:00:00', "
                "'2026-11-11 10:00:00', 'approver-1', NULL)"
            )

        config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == head_revision()
            assert connection.exec_driver_sql(
                "SELECT id, status, alternate_approver_id, reminder_sent_at "
                "FROM assistant_action_approvals"
            ).one() == ("approval-preserved", "pending", "approver-1", None)
            assert "expiry_notified_at" in {
                column["name"]
                for column in inspect(connection).get_columns("assistant_action_approvals")
            }
            assert connection.exec_driver_sql(
                "SELECT affected_people FROM assistant_action_approvals "
                "WHERE id = 'approval-preserved'"
            ).scalar_one() is None
            assert {
                index["name"] for index in inspect(connection).get_indexes(
                    "assistant_action_approvals"
                )
            } >= {"ix_assistant_action_approvals_alternate_status"}
    finally:
        engine.dispose()


def test_approval_affected_people_migration_is_retry_safe(tmp_path: Path):
    database_path = tmp_path / "approval_affected_people.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('20261109_01')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE assistant_action_approvals ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, "
                "status VARCHAR(20) NOT NULL, "
                "affected_people JSON)"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_action_approvals "
                "(id, status, affected_people) "
                "VALUES ('approval-preserved', 'pending', "
                "'[{\"name\":\"Alice Smith\",\"email\":\"alice@example.test\"}]')"
            )

        config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
        command.upgrade(config, "head")
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == head_revision()
            assert json.loads(connection.exec_driver_sql(
                "SELECT affected_people FROM assistant_action_approvals "
                "WHERE id = 'approval-preserved'"
            ).scalar_one()) == [
                {"name": "Alice Smith", "email": "alice@example.test"}
            ]
            assert "affected_people" in {
                column["name"]
                for column in inspect(connection).get_columns("assistant_action_approvals")
            }
    finally:
        engine.dispose()
