"""Administrator API tests for durable assistant run history."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AssistantActionApproval,
    AssistantConversation,
    AssistantRun,
    AssistantRunStep,
    AssistantToolExecution,
    Base,
    Employee,
    Meeting,
    WorkflowRun,
    WorkflowStep,
)

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _principal(user_id: str = "admin-1", role: str = "admin") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user_id,
        account_id=f"account-{user_id}",
        email=f"{user_id}@example.test",
        name=user_id,
        role=role,
        team="Operations",
        jti=f"jti-{user_id}",
    )


@pytest.fixture()
def client(db_session: Session):
    current = {"principal": _principal()}
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: current["principal"]
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.login = lambda user_id, role: current.update(
            principal=_principal(user_id, role)
        )
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def anonymous_client(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides.pop(require_authenticated_principal, None)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _employee(db: Session, employee_id: str, name: str, email: str) -> None:
    db.add(Employee(
        id=employee_id,
        name=name,
        email=email,
        team="Operations",
        is_active=True,
    ))
    db.commit()


def _run(
    db: Session,
    *,
    run_id: str,
    user_id: str,
    status: str,
    started_at: datetime,
    conversation_id: str | None = None,
) -> AssistantRun:
    run = AssistantRun(
        id=run_id,
        user_id=user_id,
        conversation_id=conversation_id,
        status=status,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=2) if status == "completed" else None,
        created_at=started_at,
        updated_at=started_at,
    )
    db.add(run)
    db.commit()
    return run


def _workflow_run(
    db: Session,
    *,
    run_id: str,
    status: str,
    organizer_id: str | None = None,
    started_at: datetime = NOW,
    attempt_count: int = 0,
) -> WorkflowRun:
    meeting_id = None
    if organizer_id is not None:
        meeting_id = f"meeting-{run_id}"
        db.add(Meeting(
            id=meeting_id,
            organizer_id=organizer_id,
            title="Private meeting title",
            start_at=started_at,
            end_at=started_at + timedelta(hours=1),
            join_url=f"https://example.test/meetings/{meeting_id}",
            status="completed",
        ))
    run = WorkflowRun(
        id=run_id,
        workflow_type="meeting_follow_up",
        meeting_id=meeting_id,
        idempotency_key=f"workflow:{run_id}",
        priority=10,
        status=status,
        payload={"private_payload": "do not return", "cost_usd": 0.25},
        attempt_count=attempt_count,
        max_attempts=5,
        claimed_at=started_at if status != "pending" else None,
        completed_at=started_at + timedelta(seconds=3)
        if status in ("completed", "failed")
        else None,
        created_at=started_at - timedelta(seconds=1),
        updated_at=started_at,
    )
    db.add(run)
    db.commit()
    return run


def test_unauthenticated_access_is_rejected(anonymous_client):
    assert anonymous_client.get("/api/v1/admin/assistant-runs").status_code == 401
    assert anonymous_client.get("/api/v1/admin/assistant-runs/run-1").status_code == 401
    assert anonymous_client.get("/api/v1/admin/workflow-runs").status_code == 401
    assert anonymous_client.get("/api/v1/admin/workflow-runs/workflow-1").status_code == 401


def test_non_admin_access_is_rejected(client):
    for role in ("employee", "manager", "hr_operations"):
        client.login("ordinary-user", role)
        listing = client.get("/api/v1/admin/assistant-runs")
        detail = client.get("/api/v1/admin/assistant-runs/run-1")
        assert listing.status_code == 403
        assert detail.status_code == 403
        assert set(listing.json()) == {"detail"}
        assert set(detail.json()) == {"detail"}


def test_non_admin_cannot_list_or_inspect_background_workflows(client):
    client.login("ordinary-user", "manager")
    assert client.get("/api/v1/admin/workflow-runs").status_code == 403
    assert client.get("/api/v1/admin/workflow-runs/workflow-1").status_code == 403


def test_admin_lists_runs_newest_first_and_filters_status_and_user(client, db_session):
    _employee(db_session, "user-1", "Ada North", "ada.north@example.test")
    _employee(db_session, "user-2", "Ben South", "ben.south@example.test")
    _run(
        db_session, run_id="run-old", user_id="user-1", status="completed",
        started_at=NOW - timedelta(hours=2),
    )
    _run(
        db_session, run_id="run-new", user_id="user-1", status="failed",
        started_at=NOW,
    )
    _run(
        db_session, run_id="run-other", user_id="user-2", status="failed",
        started_at=NOW - timedelta(minutes=5),
    )

    response = client.get("/api/v1/admin/assistant-runs")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [run["id"] for run in response.json()["runs"]] == [
        "run-new", "run-other", "run-old",
    ]

    filtered = client.get(
        "/api/v1/admin/assistant-runs",
        params={"status": "failed", "user": "ADA.NORTH@EXAMPLE.TEST"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert [run["id"] for run in filtered.json()["runs"]] == ["run-new"]
    assert filtered.json()["runs"][0]["user_id"] == "user-1"

    by_name = client.get("/api/v1/admin/assistant-runs", params={"user": "Ada North"})
    assert [run["id"] for run in by_name.json()["runs"]] == ["run-new", "run-old"]


def test_list_paginates_and_rejects_unknown_status(client, db_session):
    _employee(db_session, "user-1", "Ada North", "ada.north@example.test")
    for index in range(3):
        _run(
            db_session,
            run_id=f"run-{index}",
            user_id="user-1",
            status="completed",
            started_at=NOW + timedelta(seconds=index),
        )

    page = client.get(
        "/api/v1/admin/assistant-runs",
        params={"limit": 2, "offset": 1},
    )
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1
    assert page.json()["has_more"] is False
    assert [run["id"] for run in page.json()["runs"]] == ["run-1", "run-0"]

    invalid = client.get("/api/v1/admin/assistant-runs", params={"status": "running-ish"})
    assert invalid.status_code == 422


def test_list_includes_retry_and_labeled_token_aggregates(client, db_session):
    _employee(db_session, "user-1", "Ada North", "ada.north@example.test")
    run = _run(
        db_session,
        run_id="run-metrics",
        user_id="user-1",
        status="completed",
        started_at=NOW,
    )
    db_session.add_all([
        AssistantRunStep(
            id="step-metrics-1",
            run_id=run.id,
            step_order=1,
            step_type="tool",
            step_name="Search",
            status="completed",
            retry_count=1,
            estimated_tokens=12,
            provider_prompt_tokens=10,
            provider_completion_tokens=4,
            provider_total_tokens=14,
        ),
        AssistantRunStep(
            id="step-metrics-2",
            run_id=run.id,
            step_order=2,
            step_type="response",
            step_name="Response",
            status="completed",
            retry_count=0,
            estimated_tokens=8,
            provider_prompt_tokens=6,
            provider_completion_tokens=3,
            provider_total_tokens=9,
        ),
    ])
    db_session.commit()

    listed = client.get("/api/v1/admin/assistant-runs").json()["runs"][0]
    assert listed["user_name"] == "Ada North"
    assert listed["retry_count"] == 1
    assert listed["estimated_tokens"] == 20
    assert listed["provider_usage"] == {
        "prompt_tokens": 16,
        "completion_tokens": 7,
        "total_tokens": 23,
    }
    assert "cost" not in listed


def test_detail_returns_ordered_steps_pending_approvals_and_redacted_data(client, db_session):
    _employee(db_session, "user-1", "Ada North", "ada.north@example.test")
    conversation = AssistantConversation(
        id="conversation-1",
        user_id="user-1",
        title="private title",
        execution_state=json.dumps({"prompt": "private checkpoint prompt"}),
        created_at=NOW,
        updated_at=NOW,
    )
    db_session.add(conversation)
    db_session.commit()
    run = _run(
        db_session,
        run_id="run-detail",
        user_id="user-1",
        status="awaiting_approval",
        started_at=NOW,
        conversation_id=conversation.id,
    )
    execution = AssistantToolExecution(
        id="execution-1",
        tool_name="email.send",
        actor_id="user-1",
        actor_role="employee",
        conversation_id=conversation.id,
        risk_tier="high",
        arguments={"body": "raw tool argument"},
        arguments_hash="tool-hash",
        status="running",
        attempts=2,
    )
    approval = AssistantActionApproval(
        id="approval-1",
        tool_name="email.send",
        requester_id="user-1",
        requester_role="employee",
        conversation_id=conversation.id,
        idempotency_key="approval-key",
        arguments={"body": "raw approval payload"},
        arguments_hash="approval-hash",
        summary="Private approval summary",
        risk_level="high",
        risk_reasons=["Private reason"],
        approver_kind="approver",
        status="pending",
        expires_at=NOW + timedelta(hours=1),
        created_at=NOW,
        updated_at=NOW,
    )
    db_session.add_all([execution, approval])
    db_session.commit()
    db_session.add_all([
        AssistantRunStep(
            id="step-2",
            run_id=run.id,
            step_order=2,
            step_type="summarise_response",
            step_name="summarise_response",
            status="pending",
            attempt_count=0,
            retry_count=0,
            estimated_tokens=20,
            created_at=NOW,
            updated_at=NOW,
        ),
        AssistantRunStep(
            id="step-0",
            run_id=run.id,
            step_order=0,
            step_type="draft_email",
            step_name="email.send",
            status="awaiting_approval",
            input_data={
                "status": "pending",
                "arguments": {
                    "body": "raw step body",
                    "recipient_email": "private@example.test",
                },
            },
            output_data={"status": "awaiting_approval", "message": "raw output"},
            started_at=NOW,
            tool_execution_id=execution.id,
            approval_id=approval.id,
            attempt_count=2,
            retry_count=1,
            estimated_tokens=12,
            provider_prompt_tokens=None,
            provider_completion_tokens=None,
            provider_total_tokens=None,
            created_at=NOW,
            updated_at=NOW,
        ),
    ])
    db_session.commit()

    response = client.get(f"/api/v1/admin/assistant-runs/{run.id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [step["step_order"] for step in body["steps"]] == [0, 2]
    traced_step = body["steps"][0]
    assert traced_step["attempt_count"] == 2
    assert traced_step["retry_count"] == 1
    assert traced_step["tool_execution_id"] == execution.id
    assert traced_step["approval_id"] == approval.id
    assert traced_step["estimated_tokens"] == 12
    assert traced_step["provider_usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    assert "cost" not in body
    assert body["pending_approvals"] == [{
        "id": approval.id,
        "tool_name": "email.send",
        "status": "pending",
        "risk_level": "high",
        "approver_kind": "approver",
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }]
    rendered = response.text
    for private_value in (
        "raw step body",
        "private@example.test",
        "raw output",
        "raw tool argument",
        "raw approval payload",
        "Private approval summary",
        "Private reason",
        "private checkpoint prompt",
        "private title",
    ):
        assert private_value not in rendered
    assert "input_data" not in body["steps"][0]
    assert "output_data" not in body["steps"][0]


def test_detail_returns_not_found_without_disclosing_data(client):
    response = client.get("/api/v1/admin/assistant-runs/not-a-run")
    assert response.status_code == 404
    assert response.json() == {"detail": "Assistant run not found."}


def test_background_workflow_list_includes_statuses_retries_and_user_search(client, db_session):
    _employee(db_session, "organizer-1", "Ava Workflow", "ava.workflow@example.test")
    states = ("pending", "processing", "completed", "failed")
    for index, state in enumerate(states):
        _workflow_run(
            db_session,
            run_id=f"workflow-{index}",
            status=state,
            organizer_id="organizer-1",
            started_at=NOW + timedelta(seconds=index),
            attempt_count=2 if state == "failed" else 0,
        )

    all_runs = client.get("/api/v1/admin/workflow-runs")
    assert all_runs.status_code == 200
    assert all_runs.headers["cache-control"] == "no-store"
    assert [run["status"] for run in all_runs.json()["runs"]] == [
        "failed", "completed", "processing", "pending",
    ]
    assert all(run["run_type"] == "background_workflow" for run in all_runs.json()["runs"])
    assert all_runs.json()["runs"][0]["retry_count"] == 1
    assert all_runs.json()["runs"][-1]["retry_count"] == 0

    for state in states:
        filtered = client.get("/api/v1/admin/workflow-runs", params={"status": state})
        assert filtered.status_code == 200
        assert [run["status"] for run in filtered.json()["runs"]] == [state]

    by_user = client.get(
        "/api/v1/admin/workflow-runs",
        params={"user": "AVA.WORKFLOW@EXAMPLE.TEST"},
    )
    assert by_user.status_code == 200
    assert by_user.json()["total"] == 4
    assert by_user.json()["runs"][0]["user_name"] == "Ava Workflow"

    page = client.get(
        "/api/v1/admin/workflow-runs",
        params={"limit": 2, "offset": 1},
    )
    assert page.status_code == 200
    assert page.json()["has_more"] is True
    assert len(page.json()["runs"]) == 2
    assert client.get(
        "/api/v1/admin/workflow-runs",
        params={"status": "unknown"},
    ).status_code == 422


def test_background_workflow_detail_excludes_payload_checkpoints_errors_and_cost(client, db_session):
    run = _workflow_run(
        db_session,
        run_id="workflow-private",
        status="failed",
        organizer_id=None,
        attempt_count=3,
    )
    db_session.add(WorkflowStep(
        id="workflow-step-1",
        workflow_run_id=run.id,
        step_key="summary",
        step_order=1,
        status="failed",
        checkpoint={"transcript": "private checkpoint transcript"},
        attempt_count=3,
        last_error="private error containing document details",
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        updated_at=NOW,
    ))
    db_session.commit()

    response = client.get(f"/api/v1/admin/workflow-runs/{run.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run_type"] == "background_workflow"
    assert body["retry_count"] == 2
    assert body["steps"][0]["step_order"] == 1
    assert body["steps"][0]["retry_count"] == 2
    assert body["steps"][0]["duration_ms"] == 1000
    assert body["pending_approvals"] == []
    assert body["estimated_tokens"] is None
    assert body["provider_usage"] is None
    assert body["cost"] is None
    serialized = json.dumps(body)
    for private_value in (
        "private_payload",
        "private checkpoint transcript",
        "private error containing document details",
        "cost_usd",
        "0.25",
    ):
        assert private_value not in serialized
    assert "payload" not in body
    assert "checkpoint" not in body["steps"][0]


def test_completed_background_run_uses_first_step_for_attempts_and_duration(client, db_session):
    run = _workflow_run(
        db_session,
        run_id="calendar-monitor-completed",
        status="completed",
        attempt_count=0,
    )
    run.claimed_at = None
    run.completed_at = NOW + timedelta(milliseconds=14)
    step = WorkflowStep(
        id="calendar-monitor-step",
        workflow_run_id=run.id,
        step_key="calendar_monitor",
        step_order=1,
        status="completed",
        attempt_count=1,
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=14),
        updated_at=NOW + timedelta(milliseconds=14),
    )
    db_session.add(step)
    db_session.commit()

    listed = client.get(
        "/api/v1/admin/workflow-runs",
        params={"status": "completed"},
    ).json()["runs"][0]
    detail = client.get(f"/api/v1/admin/workflow-runs/{run.id}").json()

    assert listed["attempt_count"] == 1
    assert listed["retry_count"] == 0
    assert listed["started_at"] == NOW.isoformat()
    assert listed["duration_ms"] == 14
    assert detail["attempt_count"] == 1
    assert detail["retry_count"] == 0
    assert detail["duration_ms"] == 14
    assert detail["steps"][0]["attempt_count"] == 1
    assert detail["steps"][0]["duration_ms"] == 14
