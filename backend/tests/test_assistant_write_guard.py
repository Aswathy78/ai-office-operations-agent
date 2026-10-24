"""Tests for Story 6.6 — Permission, conflict and risk checks before every write.

Coverage
--------
Risk         Writes are classified Low / Medium / High from the tool's baseline and
             the call's factors (priority, deadline, team, attendees, recipients,
             sensitive data, system-wide workflows).
Permissions  Scope and resource rules deny the call with a clear reason, audited.
Conflicts    Busy slots, duplicates, closed tasks and identical pending requests
             block the write with the reason, audited.
Approval     Low risk runs autonomously; medium waits for the requester; high waits
             for a different approver; reject, cancel, expiry; re-checks at approval.
Privilege    Approved actions run with the requester's current permissions, never
             the approver's; demoted or disabled requesters cannot run them.
Bypass       No flag, foreign approval or undecided approval skips the checks.
Audit        Every decision and outcome is in assistant_audit_events.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
import app.assistant_service as assistant_service
from app import email_providers
from app.assistant_approvals import decide_approval
from app.assistant_tools import (
    ToolApprovalError,
    ToolConflictBlocked,
    ToolPermissionDenied,
    evaluate_write,
    execute_tool,
    get_registry,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AssistantActionApproval,
    AssistantAuditEvent,
    AssistantToolExecution,
    AuthAccount,
    Base,
    EmailDraft,
    Employee,
    Meeting,
    Task,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeMailProvider:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, recipient, subject, body):
        self.calls.append({"recipient": recipient, "subject": subject, "body": body})
        return email_providers.DeliveryReceipt(f"<{len(self.calls)}@provider.example>", "fake")


@pytest.fixture(autouse=True)
def mail(monkeypatch):
    provider = FakeMailProvider()
    monkeypatch.setattr(email_providers, "send_email", provider)
    return provider


PEOPLE = {
    # id: (name, email, team, role)
    "emp-mgr": ("Priya Manager", "priya@northstar.example", "Engineering", "manager"),
    "emp-bob": ("Bob Lee", "bob@northstar.example", "Engineering", "employee"),
    "emp-alice": ("Alice Smith", "alice@northstar.example", "Engineering", "employee"),
    "emp-fin-mgr": ("Farah Finance", "farah@northstar.example", "Finance", "manager"),
    "emp-hana": ("Hana Ortiz", "hana@northstar.example", "Finance", "employee"),
    "emp-hr": ("Omar HR", "omar@northstar.example", "HR", "hr_operations"),
    "emp-admin": ("Ada Admin", "ada@northstar.example", "IT", "admin"),
}


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    for employee_id, (name, email, team, _) in PEOPLE.items():
        # Every day and hour is working time, so risk never depends on when tests run.
        session.add(Employee(
            id=employee_id, name=name, email=email, team=team, is_active=True,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            working_hours_start="00:00", working_hours_end="23:59",
        ))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def person(employee_id: str, role: str | None = None) -> AuthenticatedPrincipal:
    name, email, team, default_role = PEOPLE[employee_id]
    return AuthenticatedPrincipal(
        user_id=employee_id, account_id=f"acc-{employee_id}", email=email, name=name,
        role=role or default_role, team=team, jti=f"jti-{employee_id}",
    )


@pytest.fixture()
def api(db_session):
    """A TestClient whose signed-in user can be switched with ``api.login(id)``."""
    current = {"principal": person("emp-mgr")}
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: current["principal"]
    with TestClient(app) as client:
        client.login = lambda employee_id, role=None: current.update(
            principal=person(employee_id, role)
        )
        yield client
    app.dependency_overrides.clear()


def _future(days: float = 3, hour: int = 10) -> str:
    moment = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return moment.isoformat()


def _task(**overrides) -> dict:
    return {"assignee_email": "bob@northstar.example", "title": "Quarterly report",
            "due_at": _future(5), **overrides}


def _meeting(**overrides) -> dict:
    return {"title": "Sync", "start_at": _future(4, 9), "duration_minutes": 30,
            "attendee_emails": ["bob@northstar.example"], **overrides}


def _email(**overrides) -> dict:
    return {"recipient_emails": ["bob@northstar.example"], "subject": "Release",
            "body": "The release is out.", **overrides}


def _run(client, tool, arguments, key, **body):
    return client.post(
        f"/api/v1/assistant/tools/{tool}/execute",
        json={"arguments": arguments, "idempotency_key": key, **body},
    )


def _decide(client, approval_id, decision="approve", note=None):
    return client.post(
        f"/api/v1/assistant/approvals/{approval_id}/decide",
        json={"decision": decision, "note": note},
    )


def _audit(db: Session, event: str) -> list[AssistantAuditEvent]:
    return db.query(AssistantAuditEvent).filter_by(event=event).all()


def _risk(db, principal, tool, arguments) -> tuple[str, list[str]]:
    decision = evaluate_write(db, get_registry().get(tool), principal, arguments)
    return decision.risk_level, decision.risk_reasons


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


class TestRiskClassification:
    def test_every_write_tool_has_guard_hooks(self):
        for spec in get_registry().all():
            if spec.is_write:
                assert spec.assess_risk is not None, spec.name
                assert spec.detect_conflicts is not None, spec.name
                assert spec.risk_tier in ("low", "medium", "high"), spec.name

    def test_task_levels(self, db_session):
        manager = person("emp-mgr")
        assert _risk(db_session, manager, "task.create", _task())[0] == "low"
        level, reasons = _risk(db_session, manager, "task.create", _task(priority="urgent"))
        assert level == "medium" and "priority is urgent" in reasons
        level, reasons = _risk(db_session, manager, "task.create", _task(due_at=_future(0.5)))
        assert level == "medium" and "due within 24 hours" in reasons
        hr = person("emp-hr")
        level, reasons = _risk(db_session, hr, "task.create", _task())
        assert level == "high" and any("outside your team" in r for r in reasons)

    def test_meeting_levels(self, db_session):
        bob = person("emp-bob")
        assert _risk(db_session, bob, "meeting.schedule", _meeting())[0] == "low"
        six = [f"{p[1]}" for p in PEOPLE.values()][:6]
        level, reasons = _risk(db_session, bob, "meeting.schedule", _meeting(attendee_emails=six))
        assert level == "medium" and any("attendees" in r for r in reasons)
        assert _risk(db_session, bob, "meeting.schedule", _meeting(duration_minutes=180))[0] == "medium"
        level, reasons = _risk(
            db_session, bob, "meeting.schedule",
            _meeting(attendee_emails=["guest@partner.example"]),
        )
        assert level == "high" and any("external" in r for r in reasons)

    def test_meeting_outside_working_hours_is_medium(self, db_session):
        alice = db_session.get(Employee, "emp-alice")
        alice.working_days = [0, 1, 2, 3, 4]
        alice.working_hours_start, alice.working_hours_end = "09:00", "17:00"
        db_session.commit()
        # 03:00 organization time is outside Alice's 09:00-17:00 hours on any day.
        start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
            hour=3, minute=0, second=0, microsecond=0
        )
        from app.meeting.scheduling_service import get_organization_timezone

        start = start.replace(tzinfo=get_organization_timezone()).astimezone(timezone.utc)
        level, reasons = _risk(
            db_session, person("emp-bob"), "meeting.schedule",
            _meeting(attendee_emails=["alice@northstar.example"], start_at=start.isoformat()),
        )
        assert level == "medium" and any("working hours" in r for r in reasons)

    def test_email_levels(self, db_session):
        bob = person("emp-bob")
        assert _risk(db_session, bob, "email.send", _email())[0] == "medium"
        assert _risk(db_session, bob, "email.send",
                     _email(recipient_emails=["x@outside.example"]))[0] == "high"
        level, reasons = _risk(db_session, bob, "email.send",
                               _email(body="My card is 4111 1111 1111 1111."))
        assert level == "high" and any("sensitive" in r for r in reasons)

    def test_workflow_is_always_high(self, db_session):
        assert _risk(db_session, person("emp-hr"), "workflow.run",
                     {"workflow": "task_reminders"})[0] == "high"


# ---------------------------------------------------------------------------
# Permission denials
# ---------------------------------------------------------------------------


class TestPermissionDenials:
    def test_missing_scope_is_denied_with_reason_and_audited(self, api, db_session):
        api.login("emp-bob")
        response = _run(api, "task.create", _task(), "key-deny-scope-1")
        assert response.status_code == 403
        assert "tasks.write" in response.json()["detail"]["message"]
        [event] = _audit(db_session, "permission_denied")
        assert event.actor_id == "emp-bob" and "tasks.write" in event.detail
        assert db_session.query(Task).count() == 0

    def test_manager_cannot_act_outside_own_team(self, api, db_session):
        response = _run(api, "task.create", _task(assignee_email="hana@northstar.example"),
                        "key-deny-team-01")
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "own team" in detail["message"] and detail["reasons"]
        assert _audit(db_session, "permission_denied")
        assert db_session.query(Task).count() == 0
        assert db_session.query(AssistantActionApproval).count() == 0

    def test_employee_cannot_email_everyone(self, api, db_session):
        api.login("emp-bob")
        response = _run(api, "email.send", _email(recipient_emails=[], groups=["Everyone"]),
                        "key-deny-every-1")
        assert response.status_code == 403
        assert "everyone" in response.json()["detail"]["message"]

    def test_manager_cannot_dispatch_everyones_email(self, api):
        response = _run(api, "workflow.run", {"workflow": "email_dispatch"}, "key-deny-wf-0001")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_duplicate_open_task_is_blocked(self, api, db_session):
        assert _run(api, "task.create", _task(), "key-dup-task-001").status_code == 200
        response = _run(api, "task.create", _task(), "key-dup-task-002")
        assert response.status_code == 409
        [conflict] = response.json()["detail"]["conflicts"]
        assert conflict["code"] == "duplicate_task"
        assert "already has an open task" in conflict["message"]
        assert _audit(db_session, "conflict_blocked")
        assert db_session.query(Task).count() == 1

    def test_busy_calendar_slot_is_blocked_before_booking(self, api, db_session):
        assert _run(api, "meeting.schedule", _meeting(), "key-busy-0001").status_code == 200
        api.login("emp-alice")
        response = _run(api, "meeting.schedule", _meeting(title="Clash"), "key-busy-0002")
        assert response.status_code == 409
        conflicts = response.json()["detail"]["conflicts"]
        assert conflicts[0]["code"] == "calendar_conflict"
        assert "busy" in conflicts[0]["message"] and "Free slots" in conflicts[0]["message"]
        assert db_session.query(Meeting).count() == 1

    def test_organizer_conflict_counts_too(self, api, db_session):
        _run(api, "meeting.schedule", _meeting(), "key-org-busy-01")
        # Priya (organizer) is busy even though Alice is free.
        response = _run(api, "meeting.schedule",
                        _meeting(attendee_emails=["alice@northstar.example"]), "key-org-busy-02")
        assert response.status_code == 409

    def test_duplicate_email_is_blocked(self, api, db_session):
        first = _run(api, "email.send", _email(), "key-dup-mail-001")
        assert _decide(api, first.json()["approval"]["id"]).status_code == 200
        response = _run(api, "email.send", _email(), "key-dup-mail-002")
        assert response.status_code == 409
        assert response.json()["detail"]["conflicts"][0]["code"] == "duplicate_email"

    def test_identical_request_already_pending_is_blocked(self, api, db_session):
        assert _run(api, "email.send", _email(), "key-pend-mail-01").status_code == 202
        response = _run(api, "email.send", _email(), "key-pend-mail-02")
        assert response.status_code == 409
        assert response.json()["detail"]["conflicts"][0]["code"] == "pending_approval"
        assert db_session.query(AssistantActionApproval).count() == 1

    def test_notice_on_completed_task_is_blocked(self, api, db_session):
        task_id = _run(api, "task.create", _task(notify_assignee=False),
                       "key-notice-task1").json()["result"]["task_id"]
        db_session.get(Task, task_id).status = "completed"
        db_session.commit()
        response = _run(api, "notification.send_task_notice",
                        {"task_id": task_id, "message": "Ping"}, "key-notice-0001")
        assert response.status_code == 409
        assert response.json()["detail"]["conflicts"][0]["code"] == "task_closed"


# ---------------------------------------------------------------------------
# Approval workflows
# ---------------------------------------------------------------------------


class TestApprovalWorkflow:
    def test_low_risk_runs_autonomously_and_is_audited(self, api, db_session, mail):
        response = _run(api, "task.create", _task(), "key-low-auto-001")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded" and body["risk_level"] == "low"
        assert db_session.query(Task).count() == 1
        assert _audit(db_session, "auto_approved") and _audit(db_session, "executed")
        assert db_session.query(AssistantActionApproval).count() == 0

    def test_medium_risk_waits_for_the_requester(self, api, db_session, mail):
        response = _run(api, "task.create", _task(priority="urgent"), "key-medium-0001")
        assert response.status_code == 202
        approval = response.json()["approval"]
        assert approval["status"] == "pending" and approval["approver_kind"] == "requester"
        assert db_session.query(Task).count() == 0

        # Someone else cannot confirm another person's medium-risk request.
        api.login("emp-admin")
        assert _decide(api, approval["id"]).status_code == 403
        assert db_session.query(Task).count() == 0

        api.login("emp-mgr")
        decided = _decide(api, approval["id"])
        assert decided.status_code == 200, decided.text
        assert decided.json()["approval"]["status"] == "executed"
        task = db_session.query(Task).one()
        assert task.priority == "urgent" and task.creator_id == "emp-mgr"
        # Deciding again is refused; nothing runs twice.
        assert _decide(api, approval["id"]).status_code == 409
        assert db_session.query(Task).count() == 1

    def test_high_risk_needs_someone_else(self, api, db_session, mail):
        api.login("emp-bob")
        response = _run(api, "email.send", _email(recipient_emails=["x@partner.example"]),
                        "key-high-mail-01")
        assert response.status_code == 202
        approval_id = response.json()["approval"]["id"]

        # Not the requester, not a manager of another team.
        assert _decide(api, approval_id).status_code == 403
        api.login("emp-fin-mgr")
        assert _decide(api, approval_id).status_code == 403
        assert db_session.query(EmailDraft).count() == 0

        # Bob's own manager can approve; it then runs through the e-mail workflow.
        api.login("emp-mgr")
        queue = api.get("/api/v1/assistant/approvals", params={"scope": "queue"}).json()
        assert [item["id"] for item in queue] == [approval_id]
        assert queue[0]["can_decide"] is True
        decided = _decide(api, approval_id, note="Partner is expecting it.")
        assert decided.status_code == 200, decided.text
        assert decided.json()["approval"]["status"] == "executed"
        draft = db_session.query(EmailDraft).one()
        assert draft.created_by_id == "emp-bob"  # sent as Bob, not as the approver
        assert draft.status == "queued"
        assert draft.approval_decision == "approved" and draft.decided_by == "emp-mgr"

    def test_reject_runs_nothing(self, api, db_session):
        api.login("emp-bob")
        approval_id = _run(api, "email.send", _email(recipient_emails=["x@partner.example"]),
                           "key-reject-0001").json()["approval"]["id"]
        api.login("emp-hr")
        decided = _decide(api, approval_id, decision="reject", note="Not approved.")
        assert decided.status_code == 200
        assert decided.json()["approval"]["status"] == "rejected"
        assert db_session.query(EmailDraft).count() == 0
        assert _audit(db_session, "rejected")[0].detail.endswith("Not approved.")
        # The same request (same key) cannot be re-submitted after rejection.
        api.login("emp-bob")
        again = _run(api, "email.send", _email(recipient_emails=["x@partner.example"]),
                     "key-reject-0001")
        assert again.status_code == 409 and "rejected" in again.json()["detail"]

    def test_requester_can_cancel(self, api, db_session):
        approval_id = _run(api, "email.send", _email(), "key-cancel-0001").json()["approval"]["id"]
        response = api.post(f"/api/v1/assistant/approvals/{approval_id}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert _decide(api, approval_id).status_code == 409
        assert db_session.query(EmailDraft).count() == 0

    def test_expired_approval_cannot_run(self, api, db_session):
        approval_id = _run(api, "email.send", _email(), "key-expire-0001").json()["approval"]["id"]
        approval = db_session.get(AssistantActionApproval, approval_id)
        approval.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()
        assert _decide(api, approval_id).status_code == 409
        db_session.refresh(approval)
        assert approval.status == "expired"
        assert _audit(db_session, "expired")
        assert db_session.query(EmailDraft).count() == 0

    def test_conflict_arising_before_approval_blocks_execution(self, api, db_session):
        api.login("emp-bob")
        six = [p[1] for p in PEOPLE.values() if p[1] != "bob@northstar.example"][:6]
        paused = _run(api, "meeting.schedule", _meeting(attendee_emails=six), "key-late-conf-1")
        assert paused.status_code == 202  # medium: more than 5 attendees
        approval_id = paused.json()["approval"]["id"]
        # Meanwhile Alice gets booked at the same time.
        api.login("emp-alice")
        assert _run(api, "meeting.schedule", _meeting(title="Other"), "key-late-conf-2").status_code == 200
        api.login("emp-bob")
        decided = _decide(api, approval_id)
        assert decided.status_code == 200
        body = decided.json()
        assert body["approval"]["status"] == "failed"
        assert "conflict" in body["message"].lower()
        assert db_session.query(Meeting).count() == 1

    def test_requests_are_listed_for_the_requester(self, api):
        _run(api, "email.send", _email(), "key-list-0001")
        mine = api.get("/api/v1/assistant/approvals", params={"scope": "mine"}).json()
        assert len(mine) == 1 and mine[0]["can_cancel"] is True
        assert mine[0]["summary"].startswith("Send e-mail")


# ---------------------------------------------------------------------------
# Privilege enforcement
# ---------------------------------------------------------------------------


class TestPrivilegeEnforcement:
    def test_approved_action_runs_as_requester_not_approver(self, api, db_session, mail):
        paused = _run(api, "task.create", _task(priority="urgent"), "key-priv-0001")
        approval_id = paused.json()["approval"]["id"]
        _decide(api, approval_id)
        execution = db_session.query(AssistantToolExecution).one()
        assert execution.actor_id == "emp-mgr" and execution.actor_role == "manager"

        # High risk approved by an admin: the ledger still shows the requester.
        hr = person("emp-hr")
        out = execute_tool(db_session, hr, "task.create",
                           _task(title="Cross-team", assignee_email="alice@northstar.example"),
                           idempotency_key="key-priv-0002")
        assert out.status == "approval_required"
        result = decide_approval(db_session, out.approval["id"], person("emp-admin"), "approve")
        assert result.outcome is not None
        task = db_session.get(Task, result.outcome.result["task_id"])
        assert task.creator_id == "emp-hr"
        row = db_session.get(AssistantToolExecution, result.outcome.execution_id)
        assert row.actor_id == "emp-hr" and row.actor_role == "hr_operations"

    def test_demoted_requester_cannot_have_it_run_even_by_admin(self, db_session):
        hr = person("emp-hr")
        db_session.add(AuthAccount(
            id="acc-hr", employee_id="emp-hr", email_normalized="omar@northstar.example",
            password_hash="x", role="hr_operations", is_enabled=True,
        ))
        db_session.commit()
        out = execute_tool(db_session, hr, "task.create",
                           _task(assignee_email="alice@northstar.example"),
                           idempotency_key="key-demote-0001")
        # Demoted to employee before anyone approved.
        db_session.get(AuthAccount, "acc-hr").role = "employee"
        db_session.commit()
        result = decide_approval(db_session, out.approval["id"], person("emp-admin"), "approve")
        assert result.outcome is None
        assert "tasks.write" in result.message
        assert result.approval.status == "failed"
        assert db_session.query(Task).count() == 0

    def test_disabled_requester_cannot_have_it_run(self, db_session):
        out = execute_tool(db_session, person("emp-mgr"), "task.create", _task(priority="urgent"),
                           idempotency_key="key-disabled-01")
        db_session.get(Employee, "emp-mgr").is_active = False
        db_session.commit()
        # The requester themself is now inactive: decision blocked at execution.
        result = decide_approval(db_session, out.approval["id"], person("emp-mgr"), "approve")
        assert result.outcome is None and "no longer an active employee" in result.message
        assert db_session.query(Task).count() == 0


# ---------------------------------------------------------------------------
# Bypass attempts
# ---------------------------------------------------------------------------


class TestNoBypass:
    def test_confirm_flag_does_not_skip_approval(self, api, db_session):
        response = _run(api, "email.send", _email(recipient_emails=["x@partner.example"]),
                        "key-bypass-0001", confirm=True)
        assert response.status_code == 202
        assert db_session.query(EmailDraft).count() == 0

    def test_undecided_approval_id_cannot_be_used(self, db_session):
        manager = person("emp-mgr")
        out = execute_tool(db_session, manager, "task.create", _task(priority="urgent"),
                           idempotency_key="key-bypass-0002")
        with pytest.raises(ToolApprovalError):
            execute_tool(db_session, manager, "task.create", _task(priority="urgent"),
                         idempotency_key="key-bypass-0002", approval_id=out.approval["id"])
        assert db_session.query(Task).count() == 0

    def test_approval_for_a_different_call_cannot_be_reused(self, db_session, mail):
        manager = person("emp-mgr")
        out = execute_tool(db_session, manager, "task.create", _task(priority="urgent"),
                           idempotency_key="key-bypass-0003")
        approval = db_session.get(AssistantActionApproval, out.approval["id"])
        approval.decided_by, approval.decided_by_role = "emp-mgr", "manager"
        db_session.commit()
        with pytest.raises(ToolApprovalError):
            execute_tool(db_session, manager, "task.create",
                         _task(priority="urgent", title="Something bigger"),
                         idempotency_key="key-bypass-0004", approval_id=approval.id)
        # Nor by someone else.
        with pytest.raises((ToolApprovalError, ToolPermissionDenied, ToolConflictBlocked)):
            execute_tool(db_session, person("emp-admin"), "task.create", _task(priority="urgent"),
                         idempotency_key="key-bypass-0003", approval_id=approval.id)
        assert db_session.query(Task).count() == 0

    def test_risk_rising_after_approval_requires_new_approval(self, db_session):
        manager = person("emp-mgr")
        args = _task(priority="urgent")
        out = execute_tool(db_session, manager, "task.create", args, idempotency_key="key-rise-0001")
        # Bob moves to Finance: the same call is now cross-team (denied for a manager).
        db_session.get(Employee, "emp-bob").team = "Finance"
        db_session.commit()
        result = decide_approval(db_session, out.approval["id"], manager, "approve")
        assert result.outcome is None and "own team" in result.message
        assert db_session.query(Task).count() == 0


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_full_decision_trail_is_recorded(self, api, db_session, mail):
        approval_id = _run(api, "task.create", _task(priority="urgent"),
                           "key-audit-0001").json()["approval"]["id"]
        _decide(api, approval_id)
        events = [
            e.event for e in db_session.query(AssistantAuditEvent)
            .filter_by(approval_id=approval_id).order_by(AssistantAuditEvent.created_at)
        ]
        assert events == ["approval_requested", "approved", "executed"]
        trail = api.get("/api/v1/assistant/audit", params={"approval_id": approval_id}).json()
        assert {e["event"] for e in trail} == {"approval_requested", "approved", "executed"}
        assert all(e["risk_level"] == "medium" for e in trail)

    def test_employees_only_see_their_own_audit_events(self, api, db_session):
        _run(api, "task.create", _task(assignee_email="hana@northstar.example"), "key-audit-0002")
        api.login("emp-bob")
        assert api.get("/api/v1/assistant/audit").json() == []
        api.login("emp-admin")
        assert any(e["event"] == "permission_denied" for e in api.get("/api/v1/assistant/audit").json())


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


def _chat(client, message, conversation_id=None):
    body = {"message": message, **({"conversation_id": conversation_id} if conversation_id else {})}
    response = client.post("/api/v1/assistant/chat/stream", json=body)
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]
    return events, events[0]["conversation_id"]


class TestChat:
    @pytest.fixture(autouse=True)
    def _no_llm(self, monkeypatch):
        monkeypatch.setattr(assistant_service, "stream_chat_completion",
                            lambda *a, **kw: iter(["(general chat)"]))

    def test_medium_risk_chat_email_waits_for_confirm(self, api, db_session):
        api.login("emp-bob")
        events, conversation_id = _chat(api, "Send an email to Alice about the release")
        [waiting] = [e["awaiting_confirmation"] for e in events if "awaiting_confirmation" in e]
        assert waiting["risk_level"] == "medium" and waiting["approval_id"]
        text = "".join(e.get("token", "") for e in events)
        assert "Approval required (medium risk" in text and "confirm" in text
        assert db_session.query(EmailDraft).count() == 0
        _chat(api, "confirm", conversation_id)
        assert db_session.query(EmailDraft).one().status == "queued"
        assert db_session.get(AssistantActionApproval, waiting["approval_id"]).status == "executed"

    def test_cross_team_chat_task_is_denied_with_a_clear_reason(self, api, db_session):
        events, _ = _chat(api, f"Assign the budget review to Hana by {(datetime.now(timezone.utc) + timedelta(days=9)).date()}")
        [error] = [e["step_error"] for e in events if "step_error" in e]
        assert "Permission denied" in error["message"] and "own team" in error["message"]
        assert "I couldn't continue" in "".join(e.get("token", "") for e in events)
        assert db_session.query(Task).count() == 0

    def test_high_risk_chat_request_goes_to_approvers(self, api, db_session):
        api.login("emp-hr")
        due = (datetime.now(timezone.utc) + timedelta(days=9)).date()
        events, conversation_id = _chat(api, f"Assign the onboarding checklist to Bob by {due}")
        [paused] = [e["approval_required"] for e in events if "approval_required" in e]
        assert paused["risk_level"] == "high" and paused["approver_kind"] == "approver"
        text = "".join(e.get("token", "") for e in events)
        assert "sent it for approval" in text
        # Saying "confirm" in chat cannot approve a high-risk request.
        _chat(api, "confirm", conversation_id)
        assert db_session.query(Task).count() == 0
        api.login("emp-admin")
        assert _decide(api, paused["approval_id"]).json()["approval"]["status"] == "executed"
        assert db_session.query(Task).one().creator_id == "emp-hr"
