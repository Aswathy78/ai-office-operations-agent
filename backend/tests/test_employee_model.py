import uuid

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.db.seed_employees import (
    require_development_environment,
    seed_development_employees,
)
from app.meeting.models import Base, Employee


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_employee_table_exists_and_has_expected_columns(db_session: Session):
    table_names = set(inspect(db_session.get_bind()).get_table_names())
    assert "employees" in table_names

    columns = {
        column["name"]: column
        for column in inspect(db_session.get_bind()).get_columns("employees")
    }

    assert columns["id"]["nullable"] is False
    assert columns["name"]["nullable"] is False
    assert columns["email"]["nullable"] is False
    assert columns["department"]["nullable"] is True
    assert columns["is_active"]["nullable"] is False
    assert columns["created_at"]["nullable"] is False
    unique_constraints = inspect(db_session.get_bind()).get_unique_constraints("employees")
    assert any(
        constraint.get("column_names") == ["email"]
        for constraint in unique_constraints
    )


def test_employee_record_can_be_created(db_session: Session):
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Maya Thompson",
        email="maya.thompson@northstar.example",
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()

    saved = db_session.query(Employee).filter_by(email="maya.thompson@northstar.example").one()
    assert saved.name == "Maya Thompson"
    assert saved.is_active is True
    assert saved.created_at is not None


def test_seed_development_employees_is_idempotent_and_includes_rahul(
    db_session: Session,
):
    first_run = seed_development_employees(db_session)
    second_run = seed_development_employees(db_session)

    assert len(first_run) == 11
    assert len(second_run) == 0
    assert db_session.query(Employee).count() == 11
    assert {employee.email for employee in db_session.query(Employee).all()} == {
        "manager@example.com",
        "ava.patel@northstar.example",
        "marcus.chen@northstar.example",
        "isabella.lopez@northstar.example",
        "daniel.brooks@northstar.example",
        "priyasharma.example@gmail.com",
        "noah.martin@northstar.example",
        "chloe.nguyen@northstar.example",
        "ethan.walker@northstar.example",
        "rahulexample.test@gmail.com",
        "admin@northstar.example",  # the System Administrator (Story 8.6)
    }
    rahul = db_session.query(Employee).filter_by(email="rahulexample.test@gmail.com").one()
    assert rahul.name == "Rahul"
    assert rahul.is_active is True
    assert all(employee.department is None for employee in db_session.query(Employee).all())


def test_employee_seed_preserves_department_without_inference(db_session: Session):
    seed_development_employees(db_session)
    employee = db_session.query(Employee).filter_by(
        email="manager@example.com"
    ).one()
    employee.department = "Corporate Services"
    db_session.commit()

    seed_development_employees(db_session)
    db_session.refresh(employee)
    assert employee.department == "Corporate Services"


def test_seed_assigns_team_line_managers(db_session: Session):
    seed_development_employees(db_session)
    by_email = {e.email: e for e in db_session.query(Employee).all()}
    maya = by_email["manager@example.com"]

    assert by_email["rahulexample.test@gmail.com"].manager_id == maya.id
    assert maya.manager_id is None
    assert by_email["noah.martin@northstar.example"].manager_id == (
        by_email["isabella.lopez@northstar.example"].id
    )
    assert by_email["ethan.walker@northstar.example"].manager_id == (
        by_email["chloe.nguyen@northstar.example"].id
    )


@pytest.mark.parametrize("app_env", [None, "production", "development-test"])
def test_seed_command_requires_explicit_development_environment(
    monkeypatch,
    app_env: str | None,
):
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)

    with pytest.raises(SystemExit, match="APP_ENV=development"):
        require_development_environment()


def test_seed_command_allows_development_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")

    require_development_environment()
