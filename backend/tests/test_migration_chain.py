from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

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
            ).scalar_one() == "20261103_01"

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
