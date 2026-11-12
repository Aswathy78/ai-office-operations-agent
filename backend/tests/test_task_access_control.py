from datetime import datetime, timezone
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import Base, Employee, Task


@pytest.fixture()
def task_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db: Session = session_factory()

    alice = Employee(
        id="employee-alice",
        name="Alice Employee",
        email="alice@example.test",
        department="Operations",
        is_active=True,
    )
    bob = Employee(
        id="employee-bob",
        name="Bob Employee",
        email="bob@example.test",
        department="Operations",
        manager_id="employee-alice",
        is_active=True,
    )
    db.add_all([alice, bob])
    db.flush()
    tasks = [
        Task(
            id=f"task-{employee.id}",
            title=f"{employee.name}'s task",
            assignee_id=employee.id,
            creator_id=employee.id,
            source="assistant",
            idempotency_key=str(uuid.uuid4()),
            due_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
            priority="medium",
        )
        for employee in (alice, bob)
    ]
    db.add_all(tasks)
    db.commit()

    current_principal = {
        "value": AuthenticatedPrincipal(
            user_id=alice.id,
            account_id="account-alice",
            email=alice.email,
            name=alice.name,
            role="employee",
            team=None,
            jti="session-alice",
        )
    }

    def override_get_db():
        yield db

    def override_principal():
        return current_principal["value"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = override_principal
    try:
        with TestClient(app) as client:
            yield client, current_principal
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_authenticated_principal, None)
        db.rollback()
        db.close()
        engine.dispose()


def _set_principal(current_principal, *, user_id: str, email: str, role: str):
    current_principal["value"] = AuthenticatedPrincipal(
        user_id=user_id,
        account_id=f"account-{user_id}",
        email=email,
        name=user_id,
        role=role,
        team="Engineering",
        jti=f"session-{user_id}",
    )


def test_employee_task_endpoints_ignore_another_assignee_email(task_api):
    client, principal = task_api
    own_email = "alice@example.test"
    other_email = "bob@example.test"

    own_tasks = client.get("/api/v1/tasks/", params={"assignee_email": own_email})
    assert own_tasks.status_code == 200
    assert [task["id"] for task in own_tasks.json()] == ["task-employee-alice"]

    own_tasks_without_email = client.get("/api/v1/tasks/")
    assert own_tasks_without_email.status_code == 200
    assert [task["id"] for task in own_tasks_without_email.json()] == [
        "task-employee-alice"
    ]

    spoofed_list = client.get("/api/v1/tasks/", params={"assignee_email": other_email})
    assert spoofed_list.status_code == 200
    assert [task["id"] for task in spoofed_list.json()] == ["task-employee-alice"]

    spoofed_dashboard = client.get(
        "/api/v1/tasks/dashboard",
        params={"assignee_email": other_email},
    )
    assert spoofed_dashboard.status_code == 200
    assert [task["id"] for task in spoofed_dashboard.json()] == ["task-employee-alice"]


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_manager_and_admin_task_access_is_preserved(task_api, role):
    client, principal = task_api
    _set_principal(
        principal,
        user_id="employee-alice",
        email="alice@example.test",
        role=role,
    )

    dashboard = client.get("/api/v1/tasks/dashboard")
    assert dashboard.status_code == 200
    assert {task["id"] for task in dashboard.json()} == {
        "task-employee-alice",
        "task-employee-bob",
    }

    selected_assignee = client.get(
        "/api/v1/tasks/",
        params={"assignee_email": "bob@example.test"},
    )
    assert selected_assignee.status_code == 200
    assert [task["id"] for task in selected_assignee.json()] == ["task-employee-bob"]
