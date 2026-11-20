"""Tests for Story 6.5 — Select tools & execute.

Coverage
--------
Schema     Every call is validated against the tool's JSON schema; invalid calls
           are rejected and nothing runs.
Contract   Every tool declares scopes, risk tier, timeout and retry policy; the
           registry refuses tools that do not.
Scopes     Calls are refused when the caller's role lacks a required scope, and
           managers only act on their own team.
Idempotency  Writes need a key; repeating a key replays the first result without
           acting twice; reusing a key for different arguments is a conflict.
Timeout / retry  Transient failures are retried per policy; slow handlers time out.
Execution  Tools really change the system: tasks, audit rows and assignment
           e-mails, meetings and invitations, e-mail drafts through the approval /
           delivery workflow, task notices, workflow runs.
Chat       The agent selects tools from the request, runs reads, runs low-risk
           writes, waits for "confirm" before medium-risk writes, then executes
           them exactly once.

Story 6.6 changed the approval rules (see test_assistant_write_guard.py):
medium/high-risk writes now pause as "approval_required" instead of needing a
``confirm`` flag, and task/meeting writes are low risk by default.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
import app.assistant_planner as assistant_planner
import app.assistant_service as assistant_service
import app.assistant_tools as assistant_tools
from app import email_providers
from app.assistant_approvals import decide_approval
from app.assistant_tools import (
    IdempotencyConflict,
    NO_RETRY,
    RetryPolicy,
    ToolArgumentError,
    ToolConfirmationRequired,
    ToolPermissionDenied,
    ToolRegistry,
    ToolSpec,
    ToolTimeoutError,
    ToolTransientError,
    TOOL_CATEGORIES,
    execute_tool,
    get_registry,
    validate_against_schema,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.email_delivery_service import process_due_emails
from app.meeting.models import (
    AssistantAuditEvent,
    AssistantConversation,
    AssistantToolExecution,
    Base,
    EmailDraft,
    EmailDraftEvent,
    Employee,
    Meeting,
    MeetingParticipant,
    Task,
    TaskAuditLog,
    TaskNotification,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeMailProvider:
    """Records every message handed to the mail provider (no real SMTP)."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, recipient, subject, body):
        self.calls.append({"recipient": recipient, "subject": subject, "body": body})
        return email_providers.DeliveryReceipt(f"<{len(self.calls)}@provider.example>", "fake")

    # System notices (Story 8.1 e-mails approvers when something needs approval) share the
    # provider with the e-mail under test but are not what these tests check.
    NOTICE_SUBJECTS = (
        "AI assistant action approval required",
        "AI assistant approval update",
        "Office Operations workflow failure",
    )

    @property
    def messages(self) -> list[dict]:
        return [call for call in self.calls if call["subject"] not in self.NOTICE_SUBJECTS]

    def recipients(self) -> list[str]:
        return [call["recipient"] for call in self.messages]


@pytest.fixture()
def mail(monkeypatch):
    provider = FakeMailProvider()
    monkeypatch.setattr(email_providers, "send_email", provider)
    return provider


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add_all([
        Employee(id="emp-mgr", name="Priya Manager", email="priya@northstar.example",
                 team="Engineering", department="Operations", is_active=True),
        Employee(id="emp-bob", name="Bob Lee", email="bob@northstar.example",
                 team="Engineering", department="Operations", is_active=True),
        Employee(id="emp-alice", name="Alice Smith", email="alice@northstar.example",
                 team="Engineering", department="Operations", is_active=True),
        Employee(id="emp-hana", name="Hana Ortiz", email="hana@northstar.example",
                 team="Finance", department="Operations", is_active=True),
        Employee(id="emp-maya", name="Maya Thompson", email="maya@northstar.example",
                 team="Design", department="Operations", is_active=True),
    ])
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _principal(user_id: str, role: str, team: str | None) -> AuthenticatedPrincipal:
    emails = {
        "emp-mgr": ("priya@northstar.example", "Priya Manager"),
        "emp-bob": ("bob@northstar.example", "Bob Lee"),
        "emp-hana": ("hana@northstar.example", "Hana Ortiz"),
    }
    email, name = emails[user_id]
    return AuthenticatedPrincipal(
        user_id=user_id, account_id=f"acc-{user_id}", email=email, name=name,
        role=role, team=team, jti=f"jti-{user_id}",
    )


@pytest.fixture()
def manager():
    return _principal("emp-mgr", "manager", "Engineering")


@pytest.fixture()
def employee():
    return _principal("emp-bob", "employee", "Engineering")


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *a, **kw: iter(["(general chat)"]),
    )


def _client_for(db_session: Session, principal: AuthenticatedPrincipal) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture()
def as_manager(db_session, manager):
    with _client_for(db_session, manager) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def as_employee(db_session, employee):
    with _client_for(db_session, employee) as client:
        yield client
    app.dependency_overrides.clear()


def _future(days: int = 3, hour: int = 10) -> str:
    """A moment at least ``days`` ahead, moved to the next weekday.

    People work Monday to Friday, and the write guard asks for approval when a meeting
    falls outside attendees' working days. "Four days from now" landed on a Sunday
    when the suite ran on a Wednesday, so the result must not depend on the weekday.
    """
    moment = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    while moment.weekday() >= 5:
        moment += timedelta(days=1)
    return moment.isoformat()


def _execute(client: TestClient, tool: str, arguments: dict, key: str | None = None,
             confirm: bool = False):
    body = {"arguments": arguments, "confirm": confirm}
    if key:
        body["idempotency_key"] = key
    return client.post(f"/api/v1/assistant/tools/{tool}/execute", json=body)


def _approve(client: TestClient, response) -> dict:
    """Approve the pending request in an approval_required response and return the result."""
    assert response.status_code == 202, response.text
    approval_id = response.json()["approval"]["id"]
    decided = client.post(
        f"/api/v1/assistant/approvals/{approval_id}/decide", json={"decision": "approve"}
    )
    assert decided.status_code == 200, decided.text
    return decided.json()


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]


def _of(events: list[dict], key: str) -> list:
    return [event[key] for event in events if key in event]


def _chat(client: TestClient, message: str, conversation_id: str | None = None):
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = client.post("/api/v1/assistant/chat/stream", json=body)
    assert response.status_code == 200
    events = _events(response)
    return events, events[0]["conversation_id"]


def _tokens(events: list[dict]) -> str:
    return "".join(_of(events, "token"))


# ---------------------------------------------------------------------------
# Tool contract and schema validation
# ---------------------------------------------------------------------------


class TestToolContract:
    def test_every_tool_declares_scopes_risk_timeout_and_retry(self):
        tools = get_registry().all()
        assert len(tools) >= 12
        for spec in tools:
            assert spec.required_scopes, spec.name
            assert spec.risk_tier in ("read", "low", "medium", "high"), spec.name
            assert 0 < spec.timeout_seconds <= 300, spec.name
            assert spec.retry_policy.max_attempts >= 1, spec.name
            assert spec.input_schema["type"] == "object", spec.name
            assert spec.input_schema["additionalProperties"] is False, spec.name
            assert spec.requires_idempotency_key == (spec.risk_tier != "read"), spec.name

    def test_all_required_categories_are_covered(self):
        categories = {spec.category for spec in get_registry().all()}
        assert categories == set(TOOL_CATEGORIES)

    def test_registry_refuses_incomplete_tools(self):
        base = dict(
            name="test.ok",
            category="task",
            description="test",
            input_schema={"type": "object", "additionalProperties": False, "properties": {}},
            required_scopes=frozenset({"tasks.read"}),
            risk_tier="read",
            timeout_seconds=5,
            retry_policy=NO_RETRY,
            handler=lambda ctx, args: {},
        )
        broken = [
            {"required_scopes": frozenset()},
            {"required_scopes": frozenset({"made.up"})},
            {"risk_tier": "extreme"},
            {"timeout_seconds": 0},
            {"retry_policy": RetryPolicy(max_attempts=0)},
            {"category": "unknown"},
            {"input_schema": {"type": "object", "properties": {}}},
            {"input_schema": {"type": "object", "additionalProperties": False,
                              "properties": {"x": {"type": "string", "oneOf": []}}}},
        ]
        for override in broken:
            with pytest.raises(ValueError):
                ToolRegistry().register(ToolSpec(**{**base, **override}))
        ToolRegistry().register(ToolSpec(**base))

    def test_tools_endpoint_lists_contract_and_permissions(self, as_employee):
        response = as_employee.get("/api/v1/assistant/tools")
        assert response.status_code == 200
        tools = {tool["name"]: tool for tool in response.json()["tools"]}
        task_create = tools["task.create"]
        assert task_create["required_scopes"] == ["tasks.write"]
        assert task_create["risk_tier"] == "low"
        assert task_create["requires_idempotency_key"] is True
        assert task_create["timeout_seconds"] > 0
        assert "max_attempts" in task_create["retry_policy"]
        assert task_create["permitted"] is False  # employees cannot create tasks
        assert tools["meeting.schedule"]["permitted"] is True


class TestSchemaValidation:
    schema = get_registry().get("task.create").input_schema

    def _paths(self, value) -> set[str]:
        return {error["path"] for error in validate_against_schema(value, self.schema)}

    def test_valid_arguments_pass(self):
        assert validate_against_schema(
            {"assignee_email": "bob@northstar.example", "title": "Report", "due_at": _future()},
            self.schema,
        ) == []

    def test_missing_required_unknown_property_and_bad_formats(self):
        assert "$.title" in self._paths({"assignee_email": "bob@x.io", "due_at": _future()})
        assert "$.extra" in self._paths(
            {"assignee_email": "bob@x.io", "title": "t", "due_at": _future(), "extra": 1}
        )
        assert "$.assignee_email" in self._paths(
            {"assignee_email": "not-an-email", "title": "t", "due_at": _future()}
        )
        # A date without a time or time zone is not a date-time.
        assert "$.due_at" in self._paths(
            {"assignee_email": "bob@x.io", "title": "t", "due_at": "2026-12-01"}
        )
        assert "$.priority" in self._paths(
            {"assignee_email": "bob@x.io", "title": "t", "due_at": _future(), "priority": "asap"}
        )

    def test_any_of_requires_an_assignee(self):
        assert "$" in self._paths({"title": "t", "due_at": _future()})

    def test_booleans_are_not_integers(self):
        schema = get_registry().get("meeting.schedule").input_schema
        errors = validate_against_schema(
            {"title": "t", "start_at": _future(), "duration_minutes": True,
             "attendee_emails": ["bob@northstar.example"]},
            schema,
        )
        assert [e["path"] for e in errors] == ["$.duration_minutes"]

    def test_invalid_call_is_rejected_over_http_and_nothing_runs(self, as_manager, db_session):
        response = _execute(
            as_manager, "task.create",
            {"assignee_email": "bob@northstar.example", "title": "", "due_at": "tomorrow"},
            key="key-invalid-001", confirm=True,
        )
        assert response.status_code == 422
        paths = {error["path"] for error in response.json()["detail"]["errors"]}
        assert {"$.title", "$.due_at"} <= paths
        assert db_session.query(Task).count() == 0
        assert db_session.query(AssistantToolExecution).count() == 0

    def test_cross_field_rules_are_enforced(self, as_manager):
        start = _future()
        response = _execute(
            as_manager, "calendar.check_availability",
            {"attendee_emails": ["bob@northstar.example"], "start_at": start, "end_at": start},
        )
        assert response.status_code == 422
        response = _execute(
            as_manager, "task.create",
            {"assignee_email": "bob@northstar.example", "title": "Late",
             "due_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
            key="key-past-due-01", confirm=True,
        )
        assert response.status_code == 422

    def test_unknown_tool_is_404(self, as_manager):
        assert _execute(as_manager, "task.delete_everything", {}).status_code == 404


# ---------------------------------------------------------------------------
# Scopes, confirmation and idempotency
# ---------------------------------------------------------------------------


class TestPermissionsAndIdempotency:
    def _task_args(self, **overrides) -> dict:
        return {"assignee_email": "bob@northstar.example", "title": "Quarterly report",
                "due_at": _future(), **overrides}

    def test_employee_lacks_task_write_scope(self, as_employee, db_session):
        response = _execute(as_employee, "task.create", self._task_args(),
                            key="key-employee-01", confirm=True)
        assert response.status_code == 403
        assert "tasks.write" in response.json()["detail"]["message"]
        assert db_session.query(Task).count() == 0

    def test_manager_cannot_assign_outside_own_team(self, as_manager, db_session, mail):
        response = _execute(
            as_manager, "task.create", self._task_args(assignee_email="hana@northstar.example"),
            key="key-cross-team-1", confirm=True,
        )
        assert response.status_code == 403
        assert db_session.query(Task).count() == 0

    def test_write_requires_idempotency_key(self, as_manager, db_session):
        response = _execute(as_manager, "task.create", self._task_args(), confirm=True)
        assert response.status_code == 422
        assert response.json()["detail"]["errors"][0]["path"] == "idempotency_key"
        assert db_session.query(Task).count() == 0

    def test_medium_risk_write_requires_approval(self, as_manager, db_session):
        response = _execute(as_manager, "task.create", self._task_args(priority="urgent"),
                            key="key-noconfirm", confirm=True)
        assert response.status_code == 202
        assert response.json()["status"] == "approval_required"
        assert db_session.query(Task).count() == 0

    def test_same_key_replays_instead_of_duplicating(self, as_manager, db_session, mail):
        args = self._task_args()
        first = _execute(as_manager, "task.create", args, key="key-replay-0001", confirm=True)
        second = as_manager.post(
            "/api/v1/assistant/tools/task.create/execute",
            json={"arguments": args, "confirm": True},
            headers={"Idempotency-Key": "key-replay-0001"},
        )
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["replayed"] is False
        assert second.json()["replayed"] is True
        assert second.json()["result"] == first.json()["result"]
        assert db_session.query(Task).count() == 1
        assert mail.recipients() == ["bob@northstar.example"]  # one assignment e-mail

    def test_same_key_with_different_arguments_is_a_conflict(self, as_manager, db_session, mail):
        _execute(as_manager, "task.create", self._task_args(), key="key-conflict-01", confirm=True)
        response = _execute(
            as_manager, "task.create", self._task_args(title="Something else"),
            key="key-conflict-01", confirm=True,
        )
        assert response.status_code == 409
        assert db_session.query(Task).count() == 1

    def test_keys_are_scoped_per_actor(self, db_session, manager, mail):
        other_manager = AuthenticatedPrincipal(
            user_id="emp-alice", account_id="acc-alice", email="alice@northstar.example",
            name="Alice Smith", role="manager", team="Engineering", jti="jti-alice",
        )
        execute_tool(db_session, manager, "task.create", self._task_args(),
                     idempotency_key="shared-key-001")
        execute_tool(db_session, other_manager, "task.create", self._task_args(title="Other"),
                     idempotency_key="shared-key-001")
        assert db_session.query(Task).count() == 2


# ---------------------------------------------------------------------------
# Timeout and retry policy
# ---------------------------------------------------------------------------


class TestTimeoutAndRetry:
    @pytest.fixture()
    def registry(self, monkeypatch):
        registry = ToolRegistry()
        monkeypatch.setattr(assistant_tools, "_REGISTRY", registry)
        monkeypatch.setattr(assistant_tools, "_sleep", lambda seconds: None)
        return registry

    def _spec(self, handler, **overrides) -> ToolSpec:
        return ToolSpec(**{
            "name": "task.test_tool",
            "category": "task",
            "description": "test",
            "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
            "required_scopes": frozenset({"tasks.read"}),
            "risk_tier": "read",
            "timeout_seconds": 5,
            "retry_policy": RetryPolicy(max_attempts=3, retry_on=(ToolTransientError,)),
            "handler": handler,
            **overrides,
        })

    def test_transient_failure_is_retried(self, registry, db_session, manager):
        calls = []

        def flaky(context, arguments):
            calls.append(context.attempt)
            if len(calls) < 3:
                raise ToolTransientError("provider busy")
            return {"ok": True}

        registry.register(self._spec(flaky))
        outcome = execute_tool(db_session, manager, "task.test_tool", {})
        assert outcome.status == "succeeded"
        assert outcome.attempts == 3
        assert calls == [1, 2, 3]

    def test_retries_stop_at_max_attempts(self, registry, db_session, manager):
        registry.register(self._spec(
            lambda context, arguments: (_ for _ in ()).throw(ToolTransientError("down")),
            retry_policy=RetryPolicy(max_attempts=2, retry_on=(ToolTransientError,)),
        ))
        with pytest.raises(ToolTransientError):
            execute_tool(db_session, manager, "task.test_tool", {})
        row = db_session.query(AssistantToolExecution).one()
        assert row.status == "failed" and row.attempts == 2

    def test_non_retryable_error_is_not_retried(self, registry, db_session, manager):
        calls = []

        def broken(context, arguments):
            calls.append(1)
            raise ValueError("bug")

        registry.register(self._spec(broken))
        with pytest.raises(assistant_tools.ToolExecutionError):
            execute_tool(db_session, manager, "task.test_tool", {})
        assert len(calls) == 1

    def test_slow_tool_times_out_and_late_result_is_recorded(self, registry, db_session, manager):
        registry.register(self._spec(
            lambda context, arguments: (time.sleep(0.6), {"late": True})[1],
            timeout_seconds=0.1,
        ))
        with pytest.raises(ToolTimeoutError):
            execute_tool(db_session, manager, "task.test_tool", {})
        row = db_session.query(AssistantToolExecution).one()
        assert row.status == "timed_out"
        time.sleep(1.0)
        db_session.expire_all()
        row = db_session.query(AssistantToolExecution).one()
        assert row.status == "succeeded" and row.result == {"late": True}

    def test_slow_document_search_times_out_without_returning_policy_content(
        self, db_session, manager, monkeypatch
    ):
        actual_registry = get_registry()
        search_spec = actual_registry.get("document.search")
        isolated_registry = ToolRegistry()

        def slow_search(context, arguments):
            time.sleep(0.2)
            return {"query": arguments["query"], "results": [{"snippet": "late policy"}], "count": 1}

        isolated_registry.register(
            replace(search_spec, timeout_seconds=0.05, handler=slow_search)
        )
        monkeypatch.setattr(assistant_tools, "_REGISTRY", isolated_registry)

        with pytest.raises(ToolTimeoutError, match="document.search.*0.05 seconds"):
            execute_tool(
                db_session,
                manager,
                "document.search",
                {"query": "approval policy"},
            )

        row = db_session.query(AssistantToolExecution).one()
        assert row.status == "timed_out"
        assert row.result is None

    def test_failed_write_resumes_from_saved_progress(self, registry, db_session, manager):
        seen = []

        def two_phase(context, arguments):
            seen.append(dict(context.progress))
            if "created" not in context.progress:
                context.save_progress(created="record-1")
                raise assistant_tools.ToolExecutionError("second phase failed")
            return {"record": context.progress["created"]}

        registry.register(self._spec(
            two_phase, risk_tier="low", required_scopes=frozenset({"tasks.write"}),
            retry_policy=NO_RETRY,
        ))
        with pytest.raises(assistant_tools.ToolExecutionError):
            execute_tool(db_session, manager, "task.test_tool", {}, idempotency_key="resume-key-1")
        outcome = execute_tool(db_session, manager, "task.test_tool", {},
                               idempotency_key="resume-key-1")
        assert outcome.result == {"record": "record-1"}
        assert seen == [{}, {"created": "record-1"}]
        assert db_session.query(AssistantToolExecution).count() == 1


# ---------------------------------------------------------------------------
# Real execution through each tool category
# ---------------------------------------------------------------------------


class TestRealExecution:
    def test_task_create_records_task_audit_and_assignment_email(
        self, as_manager, db_session, mail
    ):
        due = _future(days=5, hour=15)
        response = _execute(
            as_manager, "task.create",
            {"assignee_email": "bob@northstar.example", "title": "Prepare sprint demo",
             "due_at": due, "priority": "low"},
            key="key-task-real-01", confirm=True,
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["verification"]["status"] == "verified"
        assert result["verification"]["attempts"] == [{"attempt": 1, "mismatches": []}]
        assert result["step_status"] == "done"
        assert result["links"]["record"]["path"] == f"/tasks?task_id={result['task_id']}"
        assert result["notifications"][0]["status"] == "sent"

        task = db_session.get(Task, result["task_id"])
        assert task is not None
        assert task.source == "assistant"
        assert task.assignee_id == "emp-bob" and task.creator_id == "emp-mgr"
        assert task.priority == "low" and task.meeting_id is None
        assert task.assignment_email_status == "sent"
        audit = db_session.query(TaskAuditLog).filter_by(task_id=task.id).one()
        assert audit.action == "create" and audit.outcome == "applied"
        notice = db_session.query(TaskNotification).filter_by(task_id=task.id).one()
        assert notice.kind == "assignment" and notice.status == "sent"
        assert mail.recipients() == ["bob@northstar.example"]
        assert "AI Office Assistant" in mail.calls[0]["body"]

        # The task shows up in the existing task API even without a source meeting.
        listed = as_manager.get("/api/v1/tasks/", params={"assignee_email": "bob@northstar.example"})
        assert listed.status_code == 200
        [row] = listed.json()
        assert row["id"] == task.id and row["meeting_id"] is None and row["meeting_title"] is None

        execution = db_session.query(AssistantToolExecution).one()
        assert execution.status == "succeeded" and execution.result["task_id"] == task.id
        assert execution.result["verification"]["verified"] is True

    def test_verification_retries_readback_without_repeating_write(
        self, as_manager, db_session, mail, monkeypatch
    ):
        import app.assistant_verification as verification

        read_back = verification._read_back
        reads = []

        def mismatch_once(db, tool_name, result):
            actual = read_back(db, tool_name, result)
            reads.append(tool_name)
            if len(reads) == 1:
                actual["title"] = "Unexpected title"
            return actual

        monkeypatch.setattr(verification, "_read_back", mismatch_once)
        response = _execute(
            as_manager,
            "task.create",
            {"assignee_email": "bob@northstar.example", "title": "Verified task",
             "due_at": _future()},
            key="key-verify-retry-01",
        )

        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["verification"]["status"] == "verified"
        assert result["verification"]["attempts"][0]["mismatches"]
        assert result["verification"]["attempts"][1]["mismatches"] == []
        assert len(reads) == 2
        assert db_session.query(Task).count() == 1
        assert mail.recipients() == ["bob@northstar.example"]
        assert db_session.query(AssistantAuditEvent).filter_by(
            event="verification_succeeded"
        ).count() == 1

    def test_task_verification_compares_unwrapped_title_and_requested_due_at(
        self, as_manager, db_session, mail, monkeypatch
    ):
        import app.assistant_verification as verification
        from app.meeting.models import AssistantAuditEvent

        read_back = verification._read_back

        def mismatched_persisted_fields(db, tool_name, result):
            actual = read_back(db, tool_name, result)
            if tool_name == "task.create":
                actual["due_at"] = "2030-10-30T12:30:00+00:00"
            return actual

        monkeypatch.setattr(verification, "_read_back", mismatched_persisted_fields)
        response = _execute(
            as_manager,
            "task.create",
            {
                "assignee_email": "bob@northstar.example",
                "title": 'task "Prepare database migration report"',
                "due_at": "2030-10-30T11:30:00+00:00",
            },
            key="key-task-verify-fields",
        )

        assert response.status_code == 200, response.text
        result = response.json()["result"]
        check = result["verification"]
        assert check["status"] == "escalated"
        assert check["expected"]["title"] == "Prepare database migration report"
        assert check["expected"]["due_at"] == "2030-10-30T11:30:00+00:00"
        assert check["actual"]["title"] == 'task "Prepare database migration report"'
        assert check["actual"]["due_at"] == "2030-10-30T12:30:00+00:00"
        assert all(
            len(attempt["mismatches"]) == 2 for attempt in check["attempts"]
        )
        assert result["step_status"] == "escalated"
        assert db_session.query(AssistantAuditEvent).filter_by(
            event="verification_escalated"
        ).count() == 1
        assert sorted(mail.recipients()) == [
            "bob@northstar.example",
            "priya@northstar.example",
        ]

    def test_persistent_mismatch_escalates_after_replanned_readback(
        self, as_manager, db_session, mail, monkeypatch
    ):
        import app.assistant_verification as verification
        from app.meeting.models import AssistantAuditEvent

        requester_manager = db_session.get(Employee, "emp-alice")
        requester = db_session.get(Employee, "emp-mgr")
        requester.manager_id = requester_manager.id
        db_session.commit()
        read_back = verification._read_back
        reads = []

        def persistent_mismatch(db, tool_name, result):
            actual = read_back(db, tool_name, result)
            reads.append(tool_name)
            actual["title"] = "Unexpected title"
            return actual

        monkeypatch.setattr(verification, "_read_back", persistent_mismatch)
        response = _execute(
            as_manager,
            "task.create",
            {"assignee_email": "bob@northstar.example", "title": "Needs review",
             "due_at": _future()},
            key="key-verify-escalate-1",
        )

        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["verification"]["status"] == "escalated"
        assert result["verification"]["replanned"] is True
        assert len(reads) == verification.MAX_VERIFICATION_READS
        assert result["step_status"] == "escalated"
        assert sorted(mail.recipients()) == [
            "alice@northstar.example",
            "bob@northstar.example",
            "priya@northstar.example",
        ]
        assert db_session.query(AssistantAuditEvent).filter_by(
            event="verification_escalated"
        ).count() == 1

    def test_meeting_schedule_creates_meeting_and_sends_invitations(
        self, as_manager, db_session, mail
    ):
        start = _future(days=4, hour=9)
        args = {"title": "Design review", "start_at": start, "duration_minutes": 45,
                "attendee_emails": ["bob@northstar.example", "alice@northstar.example"]}
        response = _execute(as_manager, "meeting.schedule", args, key="key-meeting-01", confirm=True)
        assert response.status_code == 200, response.text
        result = response.json()["result"]

        meeting = db_session.get(Meeting, result["meeting_id"])
        assert meeting is not None and meeting.title == "Design review"
        assert meeting.organizer_id == "emp-mgr"
        participants = {
            p.email for p in db_session.query(MeetingParticipant).filter_by(meeting_id=meeting.id)
        }
        assert {"bob@northstar.example", "alice@northstar.example"} <= participants
        assert sorted(mail.recipients()) == ["alice@northstar.example", "bob@northstar.example"]
        assert all(call["subject"] == "Invitation: Design review" for call in mail.calls)

        # Same key again: no second meeting and no second round of invitations.
        replay = _execute(as_manager, "meeting.schedule", args, key="key-meeting-01", confirm=True)
        assert replay.json()["replayed"] is True
        assert db_session.query(Meeting).count() == 1
        assert len(mail.calls) == 2

        # A different key for the same slot conflicts with the meeting just booked.
        clash = _execute(as_manager, "meeting.schedule", args, key="key-meeting-02", confirm=True)
        assert clash.status_code == 409
        assert db_session.query(Meeting).count() == 1

    def test_calendar_check_availability_reports_conflicts(self, as_manager, mail):
        start = _future(days=6, hour=11)
        _execute(as_manager, "meeting.schedule",
                 {"title": "Busy", "start_at": start, "duration_minutes": 30,
                  "attendee_emails": ["alice@northstar.example"]},
                 key="key-busy-slot-1", confirm=True)
        end = (datetime.fromisoformat(start) + timedelta(minutes=30)).isoformat()
        response = _execute(as_manager, "calendar.check_availability",
                            {"attendee_emails": ["alice@northstar.example"],
                             "start_at": start, "end_at": end})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["available"] is False
        assert result["conflicts"][0]["conflicting_event_title"] == "Busy"

    def test_email_send_queues_and_dispatcher_delivers(self, as_manager, db_session, mail):
        response = _execute(
            as_manager, "email.send",
            {"recipient_emails": ["bob@northstar.example"], "subject": "Release notes",
             "body": "Hi Bob,\n\nThe release is out.\n\nPriya"},
            key="key-email-send-1", confirm=True,
        )
        assert db_session.query(EmailDraft).count() == 0  # medium risk: paused first
        result = _approve(as_manager, response)["execution"]["result"]
        assert result["outcome"] == "queued"
        assert result["verification"]["status"] == "verified"
        assert result["step_status"] == "done"
        assert result["links"]["record"]["path"] == (
            f"/api/v1/email-drafts/{result['draft_id']}"
        )
        assert result["notifications"][0]["status"] == "queued"
        draft = db_session.get(EmailDraft, result["draft_id"])
        assert draft.status == "queued" and draft.created_by_id == "emp-mgr"
        assert "queued for delivery" in assistant_planner.summarize_tool_result(
            "email.send", result
        )
        assert "sent successfully" not in assistant_planner.summarize_tool_result(
            "email.send", result
        )
        statuses = [e.to_status for e in db_session.query(EmailDraftEvent)
                    .filter_by(draft_id=draft.id).order_by(EmailDraftEvent.sequence)]
        assert statuses == ["draft", "queued"]
        assert mail.messages == []  # delivery is the dispatcher's job (approval notices aside)

        summary = process_due_emails(db_session)
        assert summary.sent == 1
        db_session.refresh(draft)
        assert draft.status == "sent"
        assert mail.recipients() == ["bob@northstar.example"]
        delivered_result = {**result, "delivery_results": draft.delivery_results}
        delivered_summary = assistant_planner.summarize_tool_result(
            "email.send", delivered_result
        )
        assert "sent successfully" in delivered_summary
        assert "queued for delivery" not in delivered_summary

    def test_email_to_external_recipient_goes_to_approval(self, as_manager, db_session, mail):
        response = _execute(
            as_manager, "email.send",
            {"recipient_emails": ["partner@outside.example"], "subject": "Hello",
             "body": "Hello partner."},
            key="key-email-ext-01", confirm=True,
        )
        assert response.status_code == 202, response.text
        approval = response.json()["approval"]
        assert approval["risk_level"] == "high" and approval["approver_kind"] == "approver"
        assert db_session.query(EmailDraft).count() == 0
        assert mail.calls == []

    def test_email_preview_does_not_persist(self, as_employee, db_session):
        response = _execute(as_employee, "email.preview",
                            {"recipient_emails": ["maya@northstar.example"],
                             "topic": "the offsite", "mode": "template"})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["subject"] and result["body"]
        assert result["approval_required"] is False
        assert db_session.query(EmailDraft).count() == 0

    def test_email_preview_requires_approval_for_one_external_recipient(
        self, as_employee, db_session
    ):
        response = _execute(
            as_employee,
            "email.preview",
            {
                "recipient_emails": ["partner@outside.example"],
                "topic": "the project kickoff",
                "mode": "template",
            },
        )

        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["approval_required"] is True
        assert len(result["recipients"]) == 1
        assert any("External recipient" in reason for reason in result["approval_reasons"])
        assert db_session.query(EmailDraft).count() == 0

    def test_directory_lookup_resolves_names_emails_and_groups(self, as_employee):
        response = _execute(as_employee, "directory.lookup_employees",
                            {"names": ["Alice Smith", "Nobody Here"],
                             "emails": ["maya@northstar.example"], "groups": ["Finance"]})
        result = response.json()["result"]
        names = {employee["name"] for employee in result["employees"]}
        assert names == {"Alice Smith", "Maya Thompson", "Hana Ortiz"}
        assert result["unresolved"] == ["Nobody Here"]

    def test_document_search_returns_ranked_passages(self, as_employee, tmp_path, monkeypatch):
        def unavailable(*args, **kwargs):
            raise RuntimeError("embedding model unavailable")

        policy = tmp_path / "policy.md"
        policy.write_text(
            "# Policy\n\nMeetings start on time.\n\n"
            "E-mail approval threshold: more than 10 recipients require approval.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", policy)
        monkeypatch.setattr(
            assistant_planner, "retrieve_semantic_document_context", unavailable
        )
        response = _execute(as_employee, "document.search",
                            {"query": "email approval threshold"})
        result = response.json()["result"]
        assert result["count"] == 1
        assert "threshold" in result["results"][0]["snippet"]

    def test_task_list_respects_role(self, as_manager, db_session, mail, employee):
        _execute(as_manager, "task.create",
                 {"assignee_email": "bob@northstar.example", "title": "Bob's task",
                  "due_at": _future()}, key="key-list-task-1", confirm=True)
        listed = _execute(as_manager, "task.list", {}).json()["result"]["tasks"]
        assert [task["title"] for task in listed] == ["Bob's task"]
        denied = _execute(as_manager, "task.list", {"assignee_email": "hana@northstar.example"})
        assert denied.status_code == 403

        app.dependency_overrides[require_authenticated_principal] = lambda: employee
        own = _execute(as_manager, "task.list", {}).json()["result"]["tasks"]
        assert [task["title"] for task in own] == ["Bob's task"]
        assert _execute(as_manager, "task.list",
                        {"assignee_email": "alice@northstar.example"}).status_code == 403

    def test_manager_cannot_read_same_team_employee_outside_reporting_line(
        self, as_manager, db_session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-other-report",
            name="Other Manager's Report",
            email="other.report@northstar.example",
            team="Engineering",
            manager_id="emp-other-manager",
            is_active=True,
        ))
        db_session.commit()
        execution_called = False

        def unexpected_execution(*_args, **_kwargs):
            nonlocal execution_called
            execution_called = True
            raise AssertionError("A denied read tool must not execute.")

        monkeypatch.setattr(assistant_tools, "_run_with_ledger", unexpected_execution)
        denied = _execute(
            as_manager,
            "task.list",
            {"assignee_email": "other.report@northstar.example"},
        )
        assert denied.status_code == 403
        assert execution_called is False
        [event] = db_session.query(AssistantAuditEvent).filter_by(
            actor_id="emp-mgr", tool_name="task.list", event="permission_denied"
        ).all()
        assert "other.report@northstar.example" not in event.detail
        assert "other.report@northstar.example" not in str(event.data)

    def test_tool_arguments_cannot_spoof_caller_identity(
        self, as_employee, db_session
    ):
        response = as_employee.post(
            "/api/v1/assistant/tools/task.list/execute",
            json={
                "arguments": {
                    "assignee_email": "alice@northstar.example",
                    "actor_role": "admin",
                    "employee_id": "emp-admin",
                }
            },
        )
        assert response.status_code == 422
        assert db_session.query(Task).count() == 0
        assert db_session.query(AssistantToolExecution).count() == 0

    def test_meeting_list_upcoming(self, as_manager, db_session, mail):
        # Every day is a working day, so the booking is low risk on any weekday (Story 6.6).
        bob = db_session.get(Employee, "emp-bob")
        bob.working_days = [0, 1, 2, 3, 4, 5, 6]
        bob.working_hours_start, bob.working_hours_end = "00:00", "23:59"
        db_session.commit()
        _execute(as_manager, "meeting.schedule",
                 {"title": "Planning", "start_at": _future(days=2), "duration_minutes": 30,
                  "attendee_emails": ["bob@northstar.example"]},
                 key="key-upcoming-01", confirm=True)
        meetings = _execute(as_manager, "meeting.list_upcoming", {"days_ahead": 7})
        assert [m["title"] for m in meetings.json()["result"]["meetings"]] == ["Planning"]

    def test_notification_is_recorded_delivered_and_retried(
        self, as_manager, db_session, mail, monkeypatch
    ):
        monkeypatch.setattr(assistant_tools, "_sleep", lambda seconds: None)
        task_id = _execute(as_manager, "task.create",
                           {"assignee_email": "bob@northstar.example", "title": "Notify me",
                            "due_at": _future(), "notify_assignee": False},
                           key="key-notify-task", confirm=True).json()["result"]["task_id"]

        attempts = []

        def flaky_provider(recipient, subject, body):
            attempts.append(recipient)
            if len(attempts) == 1:
                raise ConnectionRefusedError("SMTP down")
            return mail(recipient, subject, body)

        monkeypatch.setattr(email_providers, "send_email", flaky_provider)
        response = _execute(as_manager, "notification.send_task_notice",
                            {"task_id": task_id, "message": "Please start this today."},
                            key="key-notify-0001")
        assert response.status_code == 200, response.text
        assert response.json()["attempts"] == 2
        notice = db_session.query(TaskNotification).filter_by(kind="assistant_notice").one()
        assert notice.status == "sent"
        assert mail.recipients() == ["bob@northstar.example"]

    def test_database_query_is_read_only_and_role_filtered(self, as_manager, db_session, mail):
        _execute(as_manager, "task.create",
                 {"assignee_email": "bob@northstar.example", "title": "Visible",
                  "due_at": _future()}, key="key-db-query-01", confirm=True)
        result = _execute(as_manager, "database.query_records", {"entity": "tasks"}).json()["result"]
        assert result["count"] == 1 and result["records"][0]["title"] == "Visible"
        bad = _execute(as_manager, "database.query_records",
                       {"entity": "tasks", "status": "deleted"})
        assert bad.status_code == 422
        raw_sql = _execute(as_manager, "database.query_records",
                           {"entity": "tasks", "sql": "DROP TABLE tasks"})
        assert raw_sql.status_code == 422

    def test_workflow_run_dispatches_queued_email(self, as_manager, db_session, mail):
        draft_id = _approve(as_manager, _execute(
            as_manager, "email.send",
            {"recipient_emails": ["alice@northstar.example"], "subject": "Ping", "body": "Ping."},
            key="key-wf-email-01",
        ))["execution"]["result"]["draft_id"]
        # Managers may not dispatch everyone's e-mail; HR / Operations may, once
        # an admin approves the high-risk run.
        denied = _execute(as_manager, "workflow.run", {"workflow": "email_dispatch"},
                          key="key-wf-run-0000")
        assert denied.status_code == 403
        hr = AuthenticatedPrincipal(
            user_id="emp-hana", account_id="acc-hana", email="hana@northstar.example",
            name="Hana Ortiz", role="hr_operations", team="Finance", jti="jti-hana",
        )
        admin = AuthenticatedPrincipal(
            user_id="emp-maya", account_id="acc-maya", email="maya@northstar.example",
            name="Maya Thompson", role="admin", team="Design", jti="jti-maya",
        )
        paused = execute_tool(db_session, hr, "workflow.run", {"workflow": "email_dispatch"},
                              idempotency_key="key-wf-run-0001")
        assert paused.status == "approval_required"
        result = decide_approval(db_session, paused.approval["id"], admin, "approve")
        assert result.outcome.result["summary"]["sent"] == 1
        assert db_session.get(EmailDraft, draft_id).status == "sent"

    def test_employee_cannot_run_workflows(self, as_employee):
        response = _execute(as_employee, "workflow.run", {"workflow": "task_reminders"},
                            key="key-wf-denied-1", confirm=True)
        assert response.status_code == 403

    def test_execution_history_is_per_user(self, as_manager, db_session, employee):
        _execute(as_manager, "document.search", {"query": "approval policy"})
        history = as_manager.get("/api/v1/assistant/tools/executions").json()
        assert [row["tool_name"] for row in history] == ["document.search"]
        execution_id = history[0]["id"]
        app.dependency_overrides[require_authenticated_principal] = lambda: employee
        assert as_manager.get("/api/v1/assistant/tools/executions").json() == []
        assert as_manager.get(
            f"/api/v1/assistant/tools/executions/{execution_id}"
        ).status_code == 404


# ---------------------------------------------------------------------------
# Chat: tool selection, confirmation gate, real execution
# ---------------------------------------------------------------------------


class TestChatExecution:
    def test_manager_low_risk_task_runs_autonomously_once(
        self, as_manager, db_session, mail, no_llm
    ):
        due = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
        events, conversation_id = _chat(as_manager, f"Assign the quarterly report to Bob by {due}")
        plan = _of(events, "plan")[0]
        assert [step["tool_name"] for step in plan] == [
            "directory.lookup_employees", "task.create", None,
        ]
        # Low risk within the manager's own team: no approval needed.
        assert not _of(events, "awaiting_confirmation")
        assert not _of(events, "step_error")
        task_step = next(
            event["step_done"] for event in events
            if event.get("step_done", {}).get("index") == 1
        )
        assert task_step["status"] == "done"
        assert task_step["links"]["record"]["path"].startswith("/tasks?task_id=")
        task = db_session.query(Task).one()
        assert task.title == "quarterly report" and task.assignee_id == "emp-bob"
        assert task.source == "assistant"
        assert mail.recipients() == ["bob@northstar.example"]
        response_text = _tokens(events)
        assert "Task: quarterly report. Assignee: Bob Lee." in response_text
        assert "Assignment e-mail: sent." in response_text
        assert "task " + task.id not in response_text
        assert "Review:" not in response_text

        # Saying "confirm" afterwards cannot run the write a second time.
        _chat(as_manager, "confirm", conversation_id)
        assert db_session.query(Task).count() == 1
        execution = db_session.query(AssistantToolExecution).filter_by(tool_name="task.create").one()
        assert execution.idempotency_key.startswith(f"chat:{conversation_id}:")
        assert execution.conversation_id == conversation_id

    def test_chat_meeting_is_booked_without_approval_when_low_risk(
        self, as_employee, db_session, mail, no_llm
    ):
        # Every day is a working day for both, so the risk does not depend on the weekday.
        for employee_id in ("emp-alice", "emp-bob"):
            employee = db_session.get(Employee, employee_id)
            employee.working_days = [0, 1, 2, 3, 4, 5, 6]
            employee.working_hours_start, employee.working_hours_end = "00:00", "23:59"
        db_session.commit()
        events, conversation_id = _chat(
            as_employee, "Schedule a meeting with Alice tomorrow at 10 AM"
        )
        assert not _of(events, "awaiting_confirmation")
        meeting = db_session.query(Meeting).one()
        assert meeting.organizer_id == "emp-bob"
        assert mail.recipients() == ["alice@northstar.example"]
        assert "Scheduled" in _tokens(events)

    def test_chat_email_goes_through_the_email_workflow(
        self, as_employee, db_session, mail, no_llm
    ):
        events, conversation_id = _chat(
            as_employee, "Send an email to Maya about the project update"
        )
        assert _of(events, "awaiting_confirmation")[0]["tool_name"] == "email.send"
        assert db_session.query(EmailDraft).count() == 0

        events, _ = _chat(as_employee, "confirm", conversation_id)
        response_text = _tokens(events)
        assert "queued for delivery" in response_text
        assert "1 recipient(s)" in response_text
        assert "Your e-mails" in response_text
        assert "/api/v1/email-drafts/" not in response_text
        assert "http://127.0.0.1:8000" not in response_text
        draft = db_session.query(EmailDraft).one()
        assert draft.status == "queued"
        assert [r["email"] for r in draft.recipients] == ["maya@northstar.example"]
        process_due_emails(db_session)
        db_session.refresh(draft)
        assert draft.status == "sent" and mail.recipients() == ["maya@northstar.example"]

    def test_cancel_discards_the_pending_action(self, as_manager, db_session, mail, no_llm):
        _, conversation_id = _chat(as_manager, "Send an email to Alice about the audit")
        events, _ = _chat(as_manager, "cancel", conversation_id)
        assert "No operation was performed" in _tokens(events)
        _chat(as_manager, "confirm", conversation_id)
        assert db_session.query(EmailDraft).count() == 0

    def test_new_message_drops_the_pending_action(self, as_manager, db_session, mail, no_llm):
        _, conversation_id = _chat(as_manager, "Send an email to Maya about the audit")
        _chat(as_manager, "What can you do?", conversation_id)
        _chat(as_manager, "confirm", conversation_id)
        assert db_session.query(EmailDraft).count() == 0

    def test_expired_confirmation_runs_nothing(
        self, as_manager, db_session, mail, no_llm, monkeypatch
    ):
        _, conversation_id = _chat(as_manager, "Send an email to Alice about the audit")
        conversation = db_session.get(AssistantConversation, conversation_id)
        db_session.refresh(conversation)
        state = json.loads(conversation.execution_state)
        state["pending_confirmation"]["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        conversation.execution_state = json.dumps(state)
        db_session.commit()

        events, _ = _chat(as_manager, "confirm", conversation_id)
        assert "expired" in _tokens(events)
        assert db_session.query(EmailDraft).count() == 0

    def test_employee_task_request_is_refused_without_asking(
        self, as_employee, db_session, no_llm
    ):
        events, conversation_id = _chat(as_employee, "Assign the audit to Alice by Friday")
        assert not _of(events, "awaiting_confirmation")
        errors = _of(events, "step_error")
        assert errors and "tasks.write" in errors[0]["message"]
        assert "I haven't made any changes." in _tokens(events)
        _chat(as_employee, "confirm", conversation_id)
        assert db_session.query(Task).count() == 0

    def test_unknown_attendee_stops_before_any_write(self, as_employee, db_session, no_llm):
        events, _ = _chat(as_employee, "Schedule a meeting with Zed Quill tomorrow at 10 AM")
        assert not _of(events, "awaiting_confirmation")
        assert db_session.query(Meeting).count() == 0
