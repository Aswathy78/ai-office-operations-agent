"""Persistence and privacy tests for interactive assistant execution traces."""

from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.assistant_run_tracing import (
    create_assistant_run,
    record_assistant_run_step,
    redact_trace_data,
    resolve_run_for_approval,
    resume_assistant_run,
    set_assistant_run_status,
)
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
)
import app.assistant_service as assistant_service


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _employee(db: Session) -> None:
    db.add(Employee(
        id="trace-user",
        name="Trace User",
        email="trace-user@example.test",
        team="Operations",
        is_active=True,
    ))
    db.add(AssistantConversation(
        id="trace-conversation",
        user_id="trace-user",
        title="Trace test",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    ))
    db.commit()


def test_run_lifecycle_keeps_start_time_and_sets_end_only_when_terminal():
    engine, factory = _session()
    db = factory()
    try:
        _employee(db)
        run = create_assistant_run(
            db, user_id="trace-user", conversation_id="trace-conversation"
        )
        started = run.started_at

        assert run.status == "running"
        assert run.ended_at is None
        assert resume_assistant_run(db, run.id).started_at == started

        set_assistant_run_status(db, run.id, "awaiting_approval")
        db.refresh(run)
        assert run.status == "awaiting_approval"
        assert run.ended_at is None

        set_assistant_run_status(db, run.id, "completed")
        db.refresh(run)
        assert run.status == "completed"
        assert run.started_at == started
        assert run.ended_at is not None
        assert run.ended_at >= started
    finally:
        db.close()
        engine.dispose()


def test_steps_are_ordered_and_estimates_are_separate_from_provider_usage():
    engine, factory = _session()
    db = factory()
    try:
        _employee(db)
        run = create_assistant_run(
            db, user_id="trace-user", conversation_id="trace-conversation"
        )
        record_assistant_run_step(
            db,
            run_id=run.id,
            step_order=2,
            step_type="summarise_response",
            step_name="summarise_response",
            status="completed",
            estimated_tokens=31,
        )
        record_assistant_run_step(
            db,
            run_id=run.id,
            step_order=0,
            step_type="tool",
            step_name="task.create",
            status="completed",
            estimated_tokens=17,
        )

        steps = (
            db.query(AssistantRunStep)
            .filter(AssistantRunStep.run_id == run.id)
            .order_by(AssistantRunStep.step_order)
            .all()
        )
        assert [step.step_order for step in steps] == [0, 2]
        assert [step.estimated_tokens for step in steps] == [17, 31]
        assert all(step.provider_total_tokens is None for step in steps)
        assert all(step.provider_prompt_tokens is None for step in steps)
        assert all(step.provider_completion_tokens is None for step in steps)
    finally:
        db.close()
        engine.dispose()


def test_step_trace_links_tool_attempts_and_records_retry_count():
    engine, factory = _session()
    db = factory()
    try:
        _employee(db)
        run = create_assistant_run(
            db, user_id="trace-user", conversation_id="trace-conversation"
        )
        db.add(AssistantToolExecution(
            id="trace-execution",
            tool_name="task.create",
            actor_id="trace-user",
            actor_role="employee",
            conversation_id="trace-conversation",
            risk_tier="low",
            arguments={},
            arguments_hash="hash",
            status="succeeded",
            attempts=3,
        ))
        db.commit()

        step = record_assistant_run_step(
            db,
            run_id=run.id,
            step_order=0,
            step_type="assign_task",
            step_name="task.create",
            status="completed",
            tool_execution_id="trace-execution",
        )
        assert step.tool_execution_id == "trace-execution"
        assert step.attempt_count == 3
        assert step.retry_count == 2
    finally:
        db.close()
        engine.dispose()


def test_external_approval_resolution_updates_linked_step_and_run():
    engine, factory = _session()
    db = factory()
    try:
        _employee(db)
        run = create_assistant_run(
            db, user_id="trace-user", conversation_id="trace-conversation"
        )
        approval = AssistantActionApproval(
            id="trace-approval",
            tool_name="email.send",
            requester_id="trace-user",
            requester_role="employee",
            conversation_id="trace-conversation",
            idempotency_key="trace-key",
            arguments={},
            arguments_hash="trace-hash",
            summary="Send an approved message",
            risk_level="high",
            risk_reasons=[],
            approver_kind="approver",
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(approval)
        db.commit()
        record_assistant_run_step(
            db,
            run_id=run.id,
            step_order=0,
            step_type="draft_email",
            step_name="email.send",
            status="awaiting_approval",
            approval_id=approval.id,
            started_at=datetime.now(timezone.utc),
        )
        set_assistant_run_status(db, run.id, "awaiting_approval")

        resolve_run_for_approval(
            db, approval, status="completed", output_data={"status": "executed"}
        )

        db.refresh(run)
        step = db.query(AssistantRunStep).one()
        assert run.status == "completed"
        assert run.ended_at is not None
        assert step.approval_id == approval.id
        assert step.status == "completed"
        assert step.output_data == {"status": "executed"}
    finally:
        db.close()
        engine.dispose()


def test_trace_redacts_sensitive_values_and_omits_free_form_content():
    unsafe = {
        "tool_name": "task.create",
        "arguments": {
            "title": "Confidential project title",
            "assignee_email": "person@example.test",
            "description": "Full user prompt and task details",
            "due_date": "2026-11-08",
            "priority": 2,
        },
        "output": {
            "status": "succeeded",
            "task_id": "private-record-id",
            "body": "Sensitive document body",
            "count": 1,
        },
    }

    safe = redact_trace_data(unsafe)
    rendered = json.dumps(safe)
    assert safe["tool_name"] == "task.create"
    assert safe["arguments"]["title"] == "[REDACTED]"
    assert safe["arguments"]["assignee_email"] == "[REDACTED]"
    assert safe["arguments"]["description"] == "[REDACTED]"
    assert safe["arguments"]["due_date"] == "[REDACTED]"
    assert safe["arguments"]["priority"] == "[REDACTED]"
    assert safe["output"]["status"] == "succeeded"
    assert safe["output"]["count"] == 1
    assert "Confidential project title" not in rendered
    assert "person@example.test" not in rendered
    assert "Full user prompt" not in rendered
    assert "Sensitive document body" not in rendered
    assert "private-record-id" not in rendered


def test_chat_stream_persists_a_completed_trace_without_prompt_content(monkeypatch):
    engine, factory = _session()
    db = factory()
    _employee(db)
    principal = AuthenticatedPrincipal(
        user_id="trace-user",
        account_id="trace-account",
        email="trace-user@example.test",
        name="Trace User",
        role="employee",
        team="Operations",
        jti="trace-jti",
    )

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    monkeypatch.setattr(assistant_service, "stream_chat_completion", lambda *_args, **_kwargs: iter(["Safe reply"]))
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/chat/stream",
                json={"message": "secretly search for this exact private prompt"},
            )
        assert response.status_code == 200
        run = db.query(AssistantRun).one()
        step = db.query(AssistantRunStep).one()
        assert run.status == "completed"
        assert run.started_at is not None and run.ended_at is not None
        assert step.step_type == "assistant_response"
        assert step.status == "completed"
        assert step.estimated_tokens is not None
        assert step.provider_total_tokens is None
        assert step.input_data["response_mode"] == "assistant"
        assert "secretly search" not in json.dumps(step.input_data)
        assert "Safe reply" not in json.dumps(step.output_data)
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
