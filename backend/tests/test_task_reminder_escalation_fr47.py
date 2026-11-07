"""Comprehensive automated test suite for Story 8.5 / FR-47: Task Reminder and Escalation Workflows."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from alembic import command
from alembic.config import Config

from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    Task,
    TaskAuditLog,
    TaskNotification,
    TASK_AUDIT_OUTCOME_SYSTEM_EVENT,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
)
from app.task_reminder_service import (
    count_missed_reminders,
    current_stage,
    missed_reminders_threshold,
    run_task_reminders,
)
from app.task_router import get_task_escalations

DUE = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


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


@pytest.fixture()
def outbox(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    def fake_send(to: str, subject: str, body: str) -> None:
        sent.append((to, subject, body))

    monkeypatch.setattr("app.task_service.send_email", fake_send)
    return sent


def _employee(db: Session, name: str, manager: Employee | None = None) -> Employee:
    employee = Employee(
        id=f"emp-{uuid.uuid4().hex[:8]}",
        name=name,
        email=f"{name.lower().replace(' ', '.')}@northstar.example",
        team="Engineering",
        manager_id=manager.id if manager else None,
        is_active=True,
    )
    db.add(employee)
    db.commit()
    return employee


def _task(db: Session, assignee: Employee, due_at: datetime = DUE, title="Deploy Critical Production Patch") -> Task:
    meeting_id, recording_id, transcript_id = (str(uuid.uuid4()) for _ in range(3))
    created = due_at - timedelta(days=5)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="user-002",
        title="Sprint Planning",
        start_at=created,
        end_at=created + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(id=recording_id, meeting_id=meeting_id, status=RECORDING_STATUS_COMPLETED)
    transcript = MeetingTranscript(
        id=transcript_id,
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Prepare the release notes.",
        segments=[{"start": 0.0, "end": 2.0, "text": "Prepare the release notes.", "speaker": "Maya"}],
        completed_at=created,
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        status=ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
        completed_at=created,
    )
    item = MeetingActionItem(
        id=str(uuid.uuid4()),
        owner_name=assignee.name,
        confirmed_employee_id=assignee.id,
        ownership_status="confirmed",
        task_description=title,
        deadline_at=due_at,
        deadline_text="by Tuesday",
        priority="high",
        confidence=0.95,
        source_segment_index=0,
        source_start_seconds=0.0,
        source_end_seconds=2.0,
        source_quote=title,
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    extraction.items.append(item)
    task = Task(
        id=str(uuid.uuid4()),
        title=title,
        assignee_id=assignee.id,
        creator_id="user-002",
        source="meeting_action_item",
        source_action_item_id=item.id,
        idempotency_key=f"meeting-action-item:{item.id}",
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        source_segment_index=0,
        source_timestamp_seconds=0.0,
        due_at=due_at,
        priority="high",
        status="pending",
        assignment_email_status="sent",
        assignment_version=1,
        calendar_sequence=0,
    )
    db.add_all([meeting, recording, transcript, extraction, task])
    db.commit()
    return task


# ==============================================================================
# Scenario 1: Reminder at T-24h
# ==============================================================================
def test_scenario_01_reminder_t24h(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    task = _task(db_session, rahul)

    summary = run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert summary.reminders_sent == 1
    assert [to for to, _, _ in outbox] == [rahul.email]

    # Verify audit log
    audit_logs = db_session.query(TaskAuditLog).filter(TaskAuditLog.task_id == task.id).all()
    actions = [a.action for a in audit_logs]
    assert "reminder_scheduled" in actions
    assert "reminder_sent" in actions
    for a in audit_logs:
        assert a.actor_id == "system"
        assert a.actor_role == "system"
        assert a.outcome == TASK_AUDIT_OUTCOME_SYSTEM_EVENT


# ==============================================================================
# Scenario 2: Reminder at T-2h
# ==============================================================================
def test_scenario_02_reminder_t2h(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    task = _task(db_session, rahul)

    # First send 24h reminder
    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert len(outbox) == 1

    # Then send 2h reminder
    summary = run_task_reminders(db_session, now=DUE - timedelta(hours=2))
    assert summary.reminders_sent == 1
    assert len(outbox) == 2
    assert outbox[1][0] == rahul.email

    notifs = db_session.query(TaskNotification).filter(TaskNotification.task_id == task.id).all()
    assert {n.kind for n in notifs} == {"reminder_24h", "reminder_2h"}


# ==============================================================================
# Scenario 3: Reminder at T-0h (deadline reached)
# ==============================================================================
def test_scenario_03_reminder_t0h(db_session, outbox):
    rahul = _employee(db_session, "Rahul Assignee")
    task = _task(db_session, rahul)

    summary = run_task_reminders(db_session, now=DUE)
    assert summary.reminders_sent == 1
    assert "Overdue task:" in outbox[0][1]

    # Verify task_overdue audit log
    overdue_log = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id, TaskAuditLog.action == "task_overdue")
        .one_or_none()
    )
    assert overdue_log is not None
    assert overdue_log.outcome == TASK_AUDIT_OUTCOME_SYSTEM_EVENT


# ==============================================================================
# Scenario 4: Daily overdue reminders
# ==============================================================================
def test_scenario_04_daily_overdue_reminders(db_session, outbox):
    rahul = _employee(db_session, "Rahul Assignee")
    _task(db_session, rahul)

    # Day 1 (T-0h)
    run_task_reminders(db_session, now=DUE)
    # Day 2 (T+24h)
    run_task_reminders(db_session, now=DUE + timedelta(days=1))
    # Day 3 (T+48h)
    run_task_reminders(db_session, now=DUE + timedelta(days=2))

    assert len(outbox) == 3
    assert all(to == rahul.email for to, _, _ in outbox)


# ==============================================================================
# Scenario 5: Two missed reminders trigger escalation
# ==============================================================================
def test_scenario_05_two_missed_reminders_trigger_escalation(db_session, outbox):
    maya = _employee(db_session, "Maya Lead")
    rahul = _employee(db_session, "Rahul Member", manager=maya)
    task = _task(db_session, rahul)

    # Pre-due reminders sent
    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))
    assert len(outbox) == 2

    # At due date, 2 reminders missed -> overdue1 sent to assignee, escalation sent to manager!
    summary = run_task_reminders(db_session, now=DUE)
    assert summary.escalations_sent == 1
    assert summary.reminders_sent == 1

    recipients = [to for to, _, _ in outbox]
    assert recipients == [rahul.email, rahul.email, rahul.email, maya.email]

    # Check audit log for escalation
    esc_log = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id, TaskAuditLog.action == "escalation_triggered")
        .one_or_none()
    )
    assert esc_log is not None

    mgr_log = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id, TaskAuditLog.action == "manager_notified")
        .one_or_none()
    )
    assert mgr_log is not None


# ==============================================================================
# Scenario 6: Configurable escalation threshold
# ==============================================================================
def test_scenario_06_configurable_escalation_threshold(db_session, outbox, monkeypatch):
    monkeypatch.setenv("ESCALATION_MISSED_REMINDERS_THRESHOLD", "3")
    assert missed_reminders_threshold() == 3

    maya = _employee(db_session, "Maya Lead")
    rahul = _employee(db_session, "Rahul Member", manager=maya)
    _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))

    # At DUE: only 2 missed so far -> threshold is 3, so NO escalation yet!
    summary_at_due = run_task_reminders(db_session, now=DUE)
    assert summary_at_due.escalations_sent == 0
    assert not any(to == maya.email for to, _, _ in outbox)

    # At DUE + 24h: overdue1 is now also missed (total 3 missed) -> escalate!
    summary_at_plus24 = run_task_reminders(db_session, now=DUE + timedelta(hours=24))
    assert summary_at_plus24.escalations_sent == 1
    assert any(to == maya.email for to, _, _ in outbox)

    # Test invalid env values fallback safely
    monkeypatch.setenv("ESCALATION_MISSED_REMINDERS_THRESHOLD", "not_a_number")
    assert missed_reminders_threshold() == 2
    monkeypatch.setenv("ESCALATION_MISSED_REMINDERS_THRESHOLD", "-5")
    assert missed_reminders_threshold() == 1


# ==============================================================================
# Scenario 7: Completed tasks receive no reminders
# ==============================================================================
def test_scenario_07_completed_task_no_reminders(db_session, outbox):
    rahul = _employee(db_session, "Rahul Assignee")
    task = _task(db_session, rahul)
    task.status = TASK_STATUS_COMPLETED
    task.completed_at = datetime.now(timezone.utc)
    db_session.commit()

    summary = run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert summary.checked == 0
    assert summary.reminders_sent == 0
    assert outbox == []


# ==============================================================================
# Scenario 8: Completed tasks are not escalated
# ==============================================================================
def test_scenario_08_completed_task_not_escalated(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    task = _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))

    # Mark completed right before due date
    task.status = TASK_STATUS_COMPLETED
    task.completed_at = datetime.now(timezone.utc)
    db_session.commit()

    run_task_reminders(db_session, now=DUE)
    run_task_reminders(db_session, now=DUE + timedelta(days=1))

    assert not any(to == maya.email for to, _, _ in outbox)
    assert len(outbox) == 2  # Only the 2 pre-due reminders


# ==============================================================================
# Scenario 9: Duplicate reminder prevention
# ==============================================================================
def test_scenario_09_duplicate_reminder_prevention(db_session, outbox):
    rahul = _employee(db_session, "Rahul Assignee")
    _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert len(outbox) == 1

    # Second run within same window
    summary2 = run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert summary2.reminders_sent == 0
    assert len(outbox) == 1


# ==============================================================================
# Scenario 10: Duplicate escalation prevention
# ==============================================================================
def test_scenario_10_duplicate_escalation_prevention(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))
    run_task_reminders(db_session, now=DUE)

    escalations_first = sum(1 for to, _, _ in outbox if to == maya.email)
    assert escalations_first == 1

    # Next overdue days must NOT resend escalation to manager
    run_task_reminders(db_session, now=DUE + timedelta(days=1))
    run_task_reminders(db_session, now=DUE + timedelta(days=2))
    escalations_after = sum(1 for to, _, _ in outbox if to == maya.email)
    assert escalations_after == 1


# ==============================================================================
# Scenario 11: Task deadline modification reschedules reminders
# ==============================================================================
def test_scenario_11_deadline_modification_reschedules(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    task = _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert len(outbox) == 1

    # Manager extends deadline by 5 days
    new_due = DUE + timedelta(days=5)
    task.due_at = new_due
    task.calendar_sequence = 1
    db_session.commit()

    # Missed reminder count resets to 0
    missed = count_missed_reminders(db_session, task, now=new_due - timedelta(hours=24))
    assert missed == 0

    # New 24h reminder for new deadline
    run_task_reminders(db_session, now=new_due - timedelta(hours=24))
    assert len(outbox) == 2


# ==============================================================================
# Scenario 12: Missing manager assignment
# ==============================================================================
def test_scenario_12_missing_manager_assignment(db_session, outbox):
    loner = _employee(db_session, "Solo Worker", manager=None)
    _task(db_session, loner)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))
    summary = run_task_reminders(db_session, now=DUE)

    assert summary.skipped_no_manager == 1
    assert summary.escalations_sent == 0
    assert summary.reminders_sent == 1
    assert all(to == loner.email for to, _, _ in outbox)


# ==============================================================================
# Scenario 13: Notification delivery failure
# ==============================================================================
def test_scenario_13_notification_delivery_failure(db_session, monkeypatch):
    rahul = _employee(db_session, "Rahul Assignee")
    task = _task(db_session, rahul)

    def fail_send(to, subject, body):
        raise OSError("SMTP service temporarily unavailable")

    monkeypatch.setattr("app.task_service.send_email", fail_send)

    summary = run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert summary.failed == 1

    fail_log = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id, TaskAuditLog.action == "reminder_failed")
        .one_or_none()
    )
    assert fail_log is not None


# ==============================================================================
# Scenario 14: Retry integration with Epic 8.4
# ==============================================================================
def test_scenario_14_retry_integration(db_session, monkeypatch):
    rahul = _employee(db_session, "Rahul Assignee")
    task = _task(db_session, rahul)

    attempts = []

    def flaky_send(to, subject, body):
        attempts.append(to)
        if len(attempts) == 1:
            raise OSError("Network glitch")

    monkeypatch.setattr("app.task_service.send_email", flaky_send)

    first_run = run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert first_run.failed == 1

    # Retry on subsequent run
    second_run = run_task_reminders(db_session, now=DUE - timedelta(hours=23))
    notif = db_session.query(TaskNotification).filter(TaskNotification.task_id == task.id).one()
    assert notif.status == "sent"

    # Verify reminder_sent audit log created on retry success
    sent_log = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id, TaskAuditLog.action == "reminder_sent")
        .one_or_none()
    )
    assert sent_log is not None


# ==============================================================================
# Scenario 15: Timezone handling
# ==============================================================================
def test_scenario_15_timezone_handling(db_session):
    ist = timezone(timedelta(hours=5, minutes=30))
    due_ist = datetime(2026, 11, 20, 17, 30, tzinfo=ist)  # 12:00 UTC

    now_utc = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    stage = current_stage(due_ist, now_utc)
    assert stage is not None
    assert stage.kind == "reminder_24h"


# ==============================================================================
# Scenario 16: Concurrent scheduler execution
# ==============================================================================
def test_scenario_16_concurrent_scheduler_execution(db_session, outbox):
    rahul = _employee(db_session, "Rahul Assignee")
    task = _task(db_session, rahul)
    # Simulate another worker claiming the row as 'pending'
    db_session.add(
        TaskNotification(
            id=str(uuid.uuid4()),
            task_id=task.id,
            recipient_employee_id=rahul.id,
            kind="reminder_24h",
            subject="claimed",
            body="claimed",
            status="pending",
            idempotency_key=f"task-reminder:{task.id}:a1:d0:t24h",
        )
    )
    db_session.commit()

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    assert outbox == []


# ==============================================================================
# Scenario 17: Manager dashboard permissions
# ==============================================================================
def test_scenario_17_manager_dashboard_permissions(db_session, outbox):
    maya = _employee(db_session, "Maya Lead")
    bob = _employee(db_session, "Bob Manager")
    rahul = _employee(db_session, "Rahul Member", manager=maya)

    task = _task(db_session, rahul)

    # Trigger escalation
    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))
    run_task_reminders(db_session, now=DUE)

    # 1. Employee gets 403
    emp_principal = AuthenticatedPrincipal(
        user_id=rahul.id,
        account_id="acc-1",
        email=rahul.email,
        name=rahul.name,
        role="employee",
        team="Engineering",
        jti="j1",
    )
    with pytest.raises(HTTPException) as exc_info:
        get_task_escalations(principal=emp_principal, db=db_session)
    assert exc_info.value.status_code == 403

    # 2. Maya (direct manager) sees 1 escalation
    maya_principal = AuthenticatedPrincipal(
        user_id=maya.id,
        account_id="acc-2",
        email=maya.email,
        name=maya.name,
        role="manager",
        team="Engineering",
        jti="j2",
    )
    maya_escalations = get_task_escalations(principal=maya_principal, db=db_session)
    assert len(maya_escalations) == 1
    assert maya_escalations[0].task_id == task.id
    assert maya_escalations[0].assignee_name == rahul.name

    # 3. Bob (different manager) sees 0 escalations
    bob_principal = AuthenticatedPrincipal(
        user_id=bob.id,
        account_id="acc-3",
        email=bob.email,
        name=bob.name,
        role="manager",
        team="Sales",
        jti="j3",
    )
    bob_escalations = get_task_escalations(principal=bob_principal, db=db_session)
    assert len(bob_escalations) == 0

    # 4. Admin sees company-wide
    admin_principal = AuthenticatedPrincipal(
        user_id="admin-1",
        account_id="acc-admin",
        email="admin@example.com",
        name="Admin User",
        role="admin",
        team=None,
        jti="j4",
    )
    admin_escalations = get_task_escalations(principal=admin_principal, db=db_session)
    assert len(admin_escalations) == 1


# ==============================================================================
# Scenario 18: Audit log creation
# ==============================================================================
def test_scenario_18_audit_log_creation(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    task = _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=2))
    run_task_reminders(db_session, now=DUE)

    logs = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id)
        .order_by(TaskAuditLog.created_at)
        .all()
    )
    actions = [l.action for l in logs]
    assert "reminder_scheduled" in actions
    assert "reminder_sent" in actions
    assert "task_overdue" in actions
    assert "escalation_triggered" in actions
    assert "manager_notified" in actions


# ==============================================================================
# Scenario 19: Audit log idempotency
# ==============================================================================
def test_scenario_19_audit_log_idempotency(db_session, outbox):
    maya = _employee(db_session, "Maya Manager")
    rahul = _employee(db_session, "Rahul Assignee", manager=maya)
    task = _task(db_session, rahul)

    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=24))
    run_task_reminders(db_session, now=DUE - timedelta(hours=24))

    sent_logs = (
        db_session.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id, TaskAuditLog.action == "reminder_sent")
        .all()
    )
    assert len(sent_logs) == 1


# ==============================================================================
# Scenario 20: Migration upgrade and downgrade
# ==============================================================================
def test_scenario_20_migration_upgrade_and_downgrade(tmp_path: Path):
    db_file = tmp_path / "migration_test.sqlite"
    cfg = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file.as_posix()}")

    # This scenario is about Story 8.5's revision. It is named explicitly (not "head") so a
    # later migration does not break it.
    command.upgrade(cfg, "20261103_01")

    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        with eng.connect() as conn:
            v = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            assert v == "20261103_01"

        # Downgrade 1 revision back to 20261102_02
        command.downgrade(cfg, "-1")
        with eng.connect() as conn:
            v_down = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            assert v_down == "20261102_02"

        # Re-upgrade
        command.upgrade(cfg, "20261103_01")
        with eng.connect() as conn:
            v_re = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            assert v_re == "20261103_01"
    finally:
        eng.dispose()
