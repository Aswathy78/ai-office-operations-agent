"""Set-up problems that kept coming back, fixed at their source.

* Tests ran against the developer's real database, leaving tables that made a later
  ``alembic upgrade`` fail with "table already exists".
* The database fell behind the code after a ``git pull`` and the app failed late.
* Passwords in the seed file did not match the database, so nobody could log in.
* A mail password saved in ``.env`` was ignored until the API restarted.
* The OCR engine was installed but not found.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app import doctor, email_providers, env_refresh
from app.db import database
from app.db.migrate import (
    DatabaseMigrationError,
    current_revision,
    ensure_database_current,
    head_revision,
)
from app.documents import extraction
from app.meeting.models import AuthAccount, Base, Employee, SystemAdministrator

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _engine(tmp_path: Path):
    path = tmp_path / "app.db"
    return create_engine(f"sqlite:///{path.as_posix()}"), path


# ---------------------------------------------------------------------------
# The test suite must not touch the real database
# ---------------------------------------------------------------------------


def test_the_test_suite_uses_a_throwaway_database_not_the_real_one():
    assert database.DATABASE_PATH != database.DEFAULT_DATABASE_PATH
    assert Path(os.environ["OFFICE_OPERATIONS_DB_PATH"]) == database.DATABASE_PATH
    assert "office-operations-tests-" in str(database.DATABASE_PATH)
    assert database.DATABASE_PATH.parent.exists()


# ---------------------------------------------------------------------------
# Migrations: re-runnable, and applied automatically
# ---------------------------------------------------------------------------


def test_the_workflow_migration_survives_tables_the_app_already_created(tmp_path):
    """A database started before it was migrated has the tables; migrating must not fail."""
    engine, path = _engine(tmp_path)
    try:
        Base.metadata.create_all(bind=engine)  # what the API's start-up does
        config = _alembic(f"sqlite:///{path.as_posix()}")
        command.stamp(config, "20261101_02")  # ... but the history stops before the workflow tables

        command.upgrade(config, "head")  # used to raise: table workflow_runs already exists

        assert current_revision(engine) == head_revision()
        assert {"workflow_runs", "workflow_steps"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_head_revision_matches_what_a_fresh_upgrade_reaches(tmp_path):
    engine, path = _engine(tmp_path)
    try:
        command.upgrade(_alembic(f"sqlite:///{path.as_posix()}"), "head")
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()


def test_a_database_with_no_history_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    engine, _ = _engine(tmp_path)
    try:
        Base.metadata.create_all(bind=engine)  # built from the models: a fresh install or a test

        assert ensure_database_current(engine) == "unversioned"
        assert current_revision(engine) is None
    finally:
        engine.dispose()


def test_a_database_that_is_up_to_date_is_not_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    engine, path = _engine(tmp_path)
    try:
        command.upgrade(_alembic(f"sqlite:///{path.as_posix()}"), "head")

        assert ensure_database_current(engine) == "current"
        assert not list(tmp_path.glob("*backup*"))
    finally:
        engine.dispose()


def test_a_database_behind_the_code_is_backed_up_then_upgraded(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    engine, path = _engine(tmp_path)
    try:
        command.upgrade(_alembic(f"sqlite:///{path.as_posix()}"), "20261102_02")
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE precious (note TEXT)")
            connection.exec_driver_sql("INSERT INTO precious VALUES ('keep me')")

        assert ensure_database_current(engine) == "upgraded"

        assert current_revision(engine) == head_revision()
        backup = tmp_path / f"app_backup_before_{head_revision()}.db"
        assert backup.exists()
        old = sqlite3.connect(backup)
        try:  # the backup is the database as it was before the upgrade
            assert old.execute("select version_num from alembic_version").fetchone()[0] == "20261102_02"
            assert old.execute("select note from precious").fetchone()[0] == "keep me"
        finally:
            old.close()
    finally:
        engine.dispose()


def test_a_failed_upgrade_stops_the_app_with_the_reason_and_keeps_the_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    engine, path = _engine(tmp_path)
    try:
        command.upgrade(_alembic(f"sqlite:///{path.as_posix()}"), "20261102_02")

        def fails(*args, **kwargs):
            raise RuntimeError("table something already exists")

        monkeypatch.setattr("app.db.migrate.command.upgrade", fails)

        with pytest.raises(DatabaseMigrationError) as raised:
            ensure_database_current(engine)

        message = str(raised.value)
        assert "could not be upgraded" in message and "20261102_02" in message
        assert "table something already exists" in message
        assert "a backup is at" in message and "alembic upgrade head" in message
        assert (tmp_path / f"app_backup_before_{head_revision()}.db").exists()
        assert current_revision(engine) == "20261102_02", "a failed upgrade changes nothing"
    finally:
        engine.dispose()


def test_a_database_from_a_newer_version_is_refused_not_downgraded(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "true")
    engine, _ = _engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('29991231_99')")

        with pytest.raises(DatabaseMigrationError, match="does not know"):
            ensure_database_current(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(("setting", "env", "expected"), [
    ("false", "development", False),
    ("true", "production", True),
    ("", "development", True),
    ("", "production", False),
])
def test_automatic_migration_is_on_in_development_and_off_in_production_unless_set(
    monkeypatch, setting, env, expected,
):
    from app.db.migrate import auto_migrate_enabled

    monkeypatch.setenv("AUTO_MIGRATE", setting)
    monkeypatch.setenv("APP_ENV", env)
    assert auto_migrate_enabled() is expected


def test_the_switch_can_turn_it_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "false")
    engine, path = _engine(tmp_path)
    try:
        command.upgrade(_alembic(f"sqlite:///{path.as_posix()}"), "20261102_02")
        assert ensure_database_current(engine) == "disabled"
        assert current_revision(engine) == "20261102_02"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Demo accounts
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeds(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)  # importing the seed module defaults it
    from app.db import seed_accounts

    return seed_accounts


@pytest.fixture()
def clean_accounts():
    def wipe():
        with database.SessionLocal() as db:
            for model in (SystemAdministrator, AuthAccount, Employee):
                db.query(model).delete()
            db.commit()

    Base.metadata.create_all(bind=database.engine)
    wipe()
    yield
    wipe()


def _stored(email: str):
    with database.SessionLocal() as db:
        account = db.query(AuthAccount).filter_by(email_normalized=email.lower()).one_or_none()
        return None if account is None else (account.password_hash, account.role)


def test_startup_creates_missing_demo_accounts_but_never_overwrites_a_password(seeds, clean_accounts):
    from app.auth_service import PASSWORD_HASHER

    with database.SessionLocal() as db:
        first = seeds.ensure_default_accounts(db, reset_passwords=False, report=lambda *_: None)
    assert first["created"] == len(seeds.DEFAULT_ACCOUNTS) and first["updated"] == 0

    maya = seeds.DEFAULT_ACCOUNTS[1]
    with database.SessionLocal() as db:
        account = db.query(AuthAccount).filter_by(email_normalized=maya["email"].lower()).one()
        account.password_hash = PASSWORD_HASHER.hash("a-password-the-user-chose")
        db.commit()

    with database.SessionLocal() as db:
        again = seeds.ensure_default_accounts(db, reset_passwords=False, report=lambda *_: None)
    assert again == {"created": 0, "updated": 0}
    hashed = _stored(maya["email"])[0]
    assert PASSWORD_HASHER.verify("a-password-the-user-chose", hashed)
    assert not PASSWORD_HASHER.verify(maya["password"], hashed)


def test_the_command_line_resets_passwords_to_the_files(seeds, clean_accounts):
    from app.auth_service import PASSWORD_HASHER

    with database.SessionLocal() as db:
        seeds.ensure_default_accounts(db, reset_passwords=False, report=lambda *_: None)
    maya = seeds.DEFAULT_ACCOUNTS[1]
    with database.SessionLocal() as db:
        db.query(AuthAccount).filter_by(email_normalized=maya["email"].lower()).one().password_hash = (
            PASSWORD_HASHER.hash("drifted")
        )
        db.commit()

    with database.SessionLocal() as db:
        counts = seeds.ensure_default_accounts(db, reset_passwords=True, report=lambda *_: None)

    assert counts["updated"] == len(seeds.DEFAULT_ACCOUNTS)
    assert PASSWORD_HASHER.verify(maya["password"], _stored(maya["email"])[0])


def test_the_demo_administrator_also_gets_a_row_in_the_administrator_table(seeds, clean_accounts):
    with database.SessionLocal() as db:
        seeds.ensure_default_accounts(db, reset_passwords=False, report=lambda *_: None)
        admins = db.query(SystemAdministrator).all()
    assert [a.email_normalized for a in admins] == ["admin@northstar.example"]


@pytest.mark.parametrize(("setting", "env", "expected"), [
    ("", "development", True),
    ("", "production", False),
    ("", "", True),
    ("false", "development", False),
    ("true", "production", True),
])
def test_demo_accounts_are_created_at_startup_only_in_development_unless_set(
    seeds, monkeypatch, setting, env, expected,
):
    monkeypatch.setenv("SEED_DEFAULT_ACCOUNTS", setting)
    if env:
        monkeypatch.setenv("APP_ENV", env)
    else:
        monkeypatch.delenv("APP_ENV", raising=False)
    assert seeds.default_account_seeding_enabled() is expected


# ---------------------------------------------------------------------------
# Mail settings follow .env
# ---------------------------------------------------------------------------


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("SMTP_HOST=smtp.gmail.com\nSMTP_USERNAME=old@example.test\nSMTP_PASSWORD=oldpassword\n"
                    "OTHER_SETTING=keep\n", encoding="utf-8")
    monkeypatch.setattr(env_refresh, "ENV_FILE", path)
    monkeypatch.setattr(env_refresh, "_under_test", lambda: False)
    for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "OTHER_SETTING"):
        monkeypatch.delenv(key, raising=False)
    env_refresh.load_mail_settings(path)
    return path


def _save(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))  # a later "saved at", whatever the clock's grain


def test_saving_the_mail_password_in_env_takes_effect_without_a_restart(env_file):
    assert os.environ["SMTP_PASSWORD"] == "oldpassword"

    _save(env_file, "SMTP_HOST=smtp.gmail.com\nSMTP_USERNAME=new@example.test\nSMTP_PASSWORD=newpassword\n")

    assert env_refresh.refresh_mail_settings(env_file) is True
    assert os.environ["SMTP_PASSWORD"] == "newpassword"
    assert os.environ["SMTP_USERNAME"] == "new@example.test"
    assert env_refresh.refresh_mail_settings(env_file) is False, "nothing changed since the last look"


def test_only_mail_settings_are_reloaded(env_file, monkeypatch):
    monkeypatch.setenv("OTHER_SETTING", "from-the-environment")

    _save(env_file, "SMTP_PASSWORD=newpassword\nOTHER_SETTING=changed-in-the-file\n")
    env_refresh.refresh_mail_settings(env_file)

    assert os.environ["OTHER_SETTING"] == "from-the-environment"


def test_the_sending_code_sees_the_new_settings(env_file, monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "587")
    assert email_providers.primary_settings().password == "oldpassword"

    _save(env_file, "SMTP_HOST=smtp.gmail.com\nSMTP_USERNAME=new@example.test\nSMTP_PASSWORD=newpassword\n")

    settings = email_providers.primary_settings()
    assert (settings.username, settings.password) == ("new@example.test", "newpassword")


def test_the_file_beats_a_stale_environment_variable_at_startup(env_file, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "left-over-in-windows")

    changed = env_refresh.load_mail_settings(env_file)

    assert "SMTP_PASSWORD" in changed and os.environ["SMTP_PASSWORD"] == "oldpassword"


def test_the_reload_is_logged_without_the_password(env_file, caplog):
    _save(env_file, "SMTP_PASSWORD=super-secret-value\n")

    with caplog.at_level(logging.WARNING, logger="app.env_refresh"):
        env_refresh.refresh_mail_settings(env_file)

    assert "SMTP_PASSWORD" in caplog.text and "super-secret-value" not in caplog.text


def test_a_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(env_refresh, "_under_test", lambda: False)
    assert env_refresh.refresh_mail_settings(tmp_path / "nope.env") is False


def test_tests_never_have_their_environment_replaced_by_the_real_env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("SMTP_PASSWORD=from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("SMTP_PASSWORD", "set-by-the-test")

    assert env_refresh.load_mail_settings(path) == []
    assert env_refresh.refresh_mail_settings(path) is False
    assert os.environ["SMTP_PASSWORD"] == "set-by-the-test"


# ---------------------------------------------------------------------------
# The OCR engine
# ---------------------------------------------------------------------------


def test_the_ocr_engine_is_found_where_the_windows_installer_puts_it(monkeypatch):
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    installed = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    monkeypatch.setattr(extraction.os.path, "exists", lambda path: path == installed)

    assert extraction.tesseract_path() == installed


def test_the_ocr_engine_lookup_prefers_the_setting_then_path_then_the_install_folder(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\tools\tesseract.exe")
    monkeypatch.setattr(extraction.os.path, "exists", lambda path: True)
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    assert extraction.tesseract_path() == r"C:\tools\tesseract.exe"

    monkeypatch.setenv("TESSERACT_CMD", r"D:\mine\tesseract.exe")
    assert extraction.tesseract_path() == r"D:\mine\tesseract.exe"


def test_no_ocr_engine_is_reported_as_none(monkeypatch):
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(extraction.os.path, "exists", lambda path: False)

    assert extraction.tesseract_path() is None


# ---------------------------------------------------------------------------
# The health check
# ---------------------------------------------------------------------------


@pytest.fixture()
def doctor_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(doctor, "ENV_FILE", path)
    for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL", "SMTP_PORT", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return path


GOOD_MAIL = (
    "SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\nSMTP_USERNAME=sender@gmail.com\n"
    "SMTP_FROM_EMAIL=sender@gmail.com\nSMTP_PASSWORD=abcd efgh ijkl mnop\n"
)


def _titles(checks, status):
    return [c.title for c in checks if c.status == status]


def test_the_health_check_accepts_consistent_mail_settings(doctor_env):
    doctor_env.write_text(GOOD_MAIL, encoding="utf-8")

    checks = doctor.check_mail()

    assert [c.status for c in checks] == ["ok"] and "sender@gmail.com" in checks[0].title


@pytest.mark.parametrize(("change", "status", "words"), [
    ("SMTP_PASSWORD=short\n", "warn", "does not look like a Google app password"),
    ("SMTP_FROM_EMAIL=someone.else@gmail.com\n", "warn", "differs from SMTP_USERNAME"),
])
def test_the_health_check_names_each_mail_mistake_and_how_to_fix_it(doctor_env, change, status, words):
    lines = [l for l in GOOD_MAIL.splitlines(keepends=True) if l.split("=")[0] != change.split("=")[0]]
    doctor_env.write_text("".join(lines) + change, encoding="utf-8")

    problems = [c for c in doctor.check_mail() if c.status == status]

    assert len(problems) == 1 and words in problems[0].title
    assert problems[0].fix


def test_the_health_check_reports_missing_mail_settings(doctor_env):
    doctor_env.write_text("SMTP_HOST=smtp.gmail.com\n", encoding="utf-8")

    [check] = doctor.check_mail()

    assert check.status == "warn" and "SMTP_PASSWORD" in check.title and "SMTP_USERNAME" in check.title


def test_the_health_check_catches_a_variable_that_overrides_the_file(doctor_env, monkeypatch):
    doctor_env.write_text(GOOD_MAIL, encoding="utf-8")
    monkeypatch.setenv("SMTP_PASSWORD", "stale-value")

    failures = [c for c in doctor.check_mail() if c.status == "fail"]

    assert failures and "SMTP_PASSWORD" in failures[0].title and "Remove-Item" in failures[0].fix


def test_the_health_check_flags_missing_packages_with_the_fix(monkeypatch):
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None if name == "faster_whisper" else object())

    failure = next(c for c in doctor.check_python() if c.status == "fail")

    assert "faster-whisper" in failure.title and ".venv-py311" in failure.fix


def test_the_health_check_warns_about_an_untested_python(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(doctor.sys, "version_info", SimpleNamespace(major=3, minor=15, micro=0))

    warning = next(c for c in doctor.check_python() if c.status == "warn")

    assert "3.15" in warning.title and ".venv-py311" in warning.fix


def test_the_health_check_notices_demo_passwords_that_do_not_match(seeds, clean_accounts):
    """The exact situation that stopped logins: the file changed, the database did not."""
    from app.auth_service import PASSWORD_HASHER, normalize_email

    with database.SessionLocal() as db:
        seeds.ensure_default_accounts(db, reset_passwords=False, report=lambda *_: None)
        maya = seeds.DEFAULT_ACCOUNTS[1]
        db.query(AuthAccount).filter_by(email_normalized=normalize_email(maya["email"])).one().password_hash = (
            PASSWORD_HASHER.hash("an-older-password")
        )
        db.commit()

    [problem] = doctor.check_accounts()

    assert problem.status == "warn" and maya["email"] in problem.title
    assert "does not accept the password in seed_accounts.py" in problem.title
    assert "seed_accounts" in problem.fix

    with database.SessionLocal() as db:
        seeds.ensure_default_accounts(db, reset_passwords=True, report=lambda *_: None)
    [healthy] = doctor.check_accounts()
    assert healthy.status == "ok"


def test_the_health_check_is_quiet_in_production_about_demo_accounts(seeds, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert doctor.check_accounts() == []


def test_the_report_lists_problems_with_their_fixes_and_sets_the_exit_status(capsys):
    checks = [
        doctor.Check("ok", "Fine"),
        doctor.Check("warn", "Odd", "because", "do this"),
        doctor.Check("fail", "Broken", fix="fix that"),
    ]

    text = doctor.format_report(checks)

    assert "OK    Fine" in text and "WARN  Odd" in text and "FAIL  Broken" in text
    assert "Fix: do this" in text and "Fix: fix that" in text and "1 problem(s) and 1 warning(s)" in text
    assert doctor.format_report([doctor.Check("ok", "Fine")]).endswith("Everything looks good.")


def test_the_health_check_runs_end_to_end_and_one_broken_check_does_not_hide_the_rest(monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "check_ocr", broken)

    results = doctor.run_checks()

    assert any("A check could not run: RuntimeError: boom" in c.title for c in results)
    assert any(c.title.startswith("Python 3.") for c in results), "the other checks still ran"


# ---------------------------------------------------------------------------
# The real start-up path
# ---------------------------------------------------------------------------
# TestClient does not run the app's start-up hook in this suite, so these call the hook
# itself. Every other test turns demo-account seeding and automatic migration off, which
# is how a missing import in this path once crashed the API at start-up unnoticed.


def test_the_start_up_hook_runs_with_automatic_migration_and_demo_accounts_switched_on(
    seeds, clean_accounts, monkeypatch,
):
    import main

    monkeypatch.setenv("SEED_DEFAULT_ACCOUNTS", "true")
    monkeypatch.setenv("AUTO_MIGRATE", "true")

    main.startup()  # raises if the hook fails

    with database.SessionLocal() as db:
        accounts = {a.email_normalized: a.role for a in db.query(AuthAccount).all()}
    assert accounts["admin@northstar.example"] == "admin"
    assert accounts["manager@example.com"] == "manager"
    assert set(accounts) >= {item["email"].lower() for item in seeds.DEFAULT_ACCOUNTS}


def test_a_restart_keeps_a_password_that_was_changed(seeds, clean_accounts, monkeypatch):
    import main

    from app.auth_service import PASSWORD_HASHER

    monkeypatch.setenv("SEED_DEFAULT_ACCOUNTS", "true")
    main.startup()
    with database.SessionLocal() as db:
        db.query(AuthAccount).filter_by(email_normalized="manager@example.com").one().password_hash = (
            PASSWORD_HASHER.hash("chosen-by-the-user")
        )
        db.commit()

    main.startup()  # a second start

    assert PASSWORD_HASHER.verify("chosen-by-the-user", _stored("manager@example.com")[0])


def test_start_up_does_not_log_the_demo_passwords(seeds, clean_accounts, monkeypatch, caplog):
    import main

    monkeypatch.setenv("SEED_DEFAULT_ACCOUNTS", "true")

    with caplog.at_level("INFO", logger="main"):
        main.startup()

    assert "[CREATED]" in caplog.text
    assert all(item["password"] not in caplog.text for item in seeds.DEFAULT_ACCOUNTS)
