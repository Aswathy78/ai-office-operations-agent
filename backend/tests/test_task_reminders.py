"""Story 4.4 (FR-21): automatic reminder and escalation e-mails for tasks."""
from datetime import datetime, timedelta, timezone
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import task_reminder_scheduler
from app.db.database import get_db
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
    TaskNotification,
    UserNotification,
)
from app.task_reminder_service import count_missed_reminders, current_stage, run_task_reminders

DUE = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
MANAGER = {"actor_id": "user-002", "actor_role": "manager", "actor_team": "Engineering"}
EMPLOYEE = {"actor_id": "user-001", "actor_role": "employee", "actor_team": "Engineering"}


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
def client(db_session: Session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def outbox(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "app.task_service.send_email",
        lambda recipient, subject, body: sent.append((recipient, subject, body)),
    )
    return sent


def _employee(db: Session, name: str, manager: Employee | None = None) -> Employee:
    employee = Employee(
        id=str(uuid.uuid4()),
        name=name,
        email=f"{name.lower().replace(' ', '.')}@northstar.example",
        team="Engineering",
        manager_id=manager.id if manager else None,
        is_active=True,
    )
    db.add(employee)
    db.commit()
    return employee


@pytest.fixture()
def maya(db_session):
    return _employee(db_session, "Maya Thompson")


@pytest.fixture()
def rahul(db_session, maya):
    return _employee(db_session, "Rahul Kumar", manager=maya)


def _task(db: Session, assignee: Employee, due_at: datetime = DUE, title="Prepare the release notes") -> Task:
    meeting_id, recording_id, transcript_id = (str(uuid.uuid4()) for _ in range(3))
    created = due_at - timedelta(days=5)
    meeting = Meeting(
        id=meeting_id, organizer_id="user-002", title="Sprint Planning",
        start_at=created, end_at=created + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}", status="completed",
    )
    recording = MeetingRecording(id=recording_id, meeting_id=meeting_id, status=RECORDING_STATUS_COMPLETED)
    transcript = MeetingTranscript(
        id=transcript_id, recording_id=recording_id, status=TRANSCRIPT_STATUS_COMPLETED,
        language="en", transcript_text="Prepare the release notes.",
        segments=[{"start": 0.0, "end": 2.0, "text": "Prepare the release notes.", "speaker": "Maya"}],
        completed_at=created,
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()), meeting_id=meeting_id, transcript_id=transcript_id,
        status=ACTION_ITEM_EXTRACTION_STATUS_COMPLETED, completed_at=created,
    )
    item = MeetingActionItem(
        id=str(uuid.uuid4()), owner_name=assignee.name, confirmed_employee_id=assignee.id,
        ownership_status="confirmed", task_description=title, deadline_at=due_at,
        deadline_text="by Tuesday", priority="high", confidence=0.95, source_segment_index=0,
        source_start_seconds=0.0, source_end_seconds=2.0, source_quote=title,
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    extraction.items.append(item)
    task = Task(
        id=str(uuid.uuid4()), title=title, assignee_id=assignee.id, creator_id="user-002",
        source="meeting_action_item", source_action_item_id=item.id,
        idempotency_key=f"meeting-action-item:{item.id}", meeting_id=meeting_id,
        transcript_id=transcript_id, source_segment_index=0, source_timestamp_seconds=0.0,
        due_at=due_at, priority="high", status="pending", assignment_email_status="sent",
    )
    db.add_all([meeting, recording, transcript, extraction, task])
    db.commit()
    return task


def _run(db: Session, at: datetime):
    return run_task_reminders(db, now=at)


def _notifications(db: Session, task: Task) -> list[TaskNotification]:
    db.expire_all()
    return (
        db.query(TaskNotification)
        .filter(TaskNotification.task_id == task.id)
        .order_by(TaskNotification.created_at)
        .all()
    )


# --- Stage calculation ------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(hours=-25), None),
        (timedelta(hours=-24), ("reminder_24h", 0)),
        (timedelta(hours=-3), ("reminder_24h", 0)),
        (timedelta(hours=-2), ("reminder_2h", 0)),
        (timedelta(minutes=-11), ("reminder_2h", 0)),
        # Story 8.5 added a last reminder in the final ten minutes before the due time.
        (timedelta(minutes=-10), ("reminder_10min", 0)),
        (timedelta(minutes=-1), ("reminder_10min", 0)),
        (timedelta(0), ("reminder_overdue", 1)),
        (timedelta(hours=23, minutes=59), ("reminder_overdue", 1)),
        (timedelta(hours=24), ("reminder_overdue", 2)),
        (timedelta(days=3, hours=1), ("reminder_overdue", 4)),
    ],
)
def test_current_stage(offset, expected):
    stage = current_stage(DUE, DUE + offset)
    assert (None if stage is None else (stage.kind, stage.day)) == expected


# --- Reminders ---------------------------------------------------------------


def test_no_email_more_than_24_hours_before_due(db_session, rahul, outbox):
    _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=30))
    assert outbox == []


def test_24h_reminder_goes_to_assignee_once(db_session, rahul, outbox):
    task = _task(db_session, rahul)

    first = _run(db_session, DUE - timedelta(hours=24))
    _run(db_session, DUE - timedelta(hours=23))  # next scheduler tick, same stage
    _run(db_session, DUE - timedelta(hours=10))

    assert first.reminders_sent == 1
    assert len(outbox) == 1
    to, subject, body = outbox[0]
    assert to == rahul.email
    assert subject == "Reminder: Prepare the release notes is due in about 24 hours"
    assert "20 Oct 2026 12:00 UTC" in body
    assert [(n.kind, n.status) for n in _notifications(db_session, task)] == [("reminder_24h", "sent")]


def test_2h_reminder_sent_after_24h_reminder(db_session, rahul, outbox):
    task = _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=24))
    _run(db_session, DUE - timedelta(hours=2))
    _run(db_session, DUE - timedelta(hours=1))

    assert [n.kind for n in _notifications(db_session, task)] == ["reminder_24h", "reminder_2h"]
    assert outbox[1][1] == "Reminder: Prepare the release notes is due in about 2 hours"


def test_missed_stages_are_not_sent_late(db_session, rahul, outbox):
    """Scheduler first sees the task 1h before due: only the current (2h) reminder goes out."""
    task = _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=1))
    assert [n.kind for n in _notifications(db_session, task)] == ["reminder_2h"]


def test_daily_overdue_reminders_while_incomplete(db_session, rahul, outbox):
    task = _task(db_session, rahul)
    for at in (
        DUE,
        DUE + timedelta(hours=5),
        DUE + timedelta(hours=24),
        DUE + timedelta(hours=30),
        DUE + timedelta(hours=48),
    ):
        _run(db_session, at)

    overdue = [n for n in _notifications(db_session, task) if n.kind == "reminder_overdue"]
    assert [n.idempotency_key.rsplit(":", 1)[-1] for n in overdue] == ["overdue1", "overdue2", "overdue3"]
    subjects = [s for to, s, _ in outbox if to == rahul.email]
    assert subjects == ["Overdue task: Prepare the release notes"] * 3
    bodies = [b for to, _, b in outbox if to == rahul.email]
    assert "is now overdue" in bodies[0]
    assert "is overdue by 1 day" in bodies[1]
    assert "is overdue by 2 days" in bodies[2]


# --- Escalation --------------------------------------------------------------


def test_escalation_to_manager_after_two_missed_reminders(db_session, rahul, maya, outbox):
    task = _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=24))
    _run(db_session, DUE - timedelta(hours=2))
    assert all(to == rahul.email for to, _, _ in outbox)  # not yet missed

    summary = _run(db_session, DUE)

    assert summary.escalations_sent == 1
    escalations = [(to, s, b) for to, s, b in outbox if to == maya.email]
    assert len(escalations) == 1
    _, subject, body = escalations[0]
    assert subject == 'Escalation: Rahul Kumar has not completed "Prepare the release notes"'
    assert "Task: Prepare the release notes" in body
    assert f"Assignee: Rahul Kumar <{rahul.email}>" in body
    assert "Due date: 20 Oct 2026 12:00 UTC" in body
    assert "2 reminders" in body


def test_escalation_inbox_delivery_and_idempotency_after_two_missed_windows(
    db_session, rahul, maya, outbox, monkeypatch
):
    task = _task(db_session, rahul)
    clock = [DUE - timedelta(hours=24)]
    monkeypatch.setattr("app.notification_service._now", lambda: clock[0])

    for at in (DUE - timedelta(hours=24), DUE - timedelta(hours=2)):
        clock[0] = at
        _run(db_session, at)

    clock[0] = DUE
    assert count_missed_reminders(db_session, task, DUE) == 2
    first_pass = _run(db_session, DUE)

    assert first_pass.escalations_sent == 1
    assert db_session.query(TaskNotification).filter_by(
        task_id=task.id,
        kind="escalation",
        recipient_employee_id=maya.id,
        status="sent",
    ).count() == 1
    inbox_item = (
        db_session.query(UserNotification)
        .filter_by(
            recipient_employee_id=maya.id,
            event_type="task_escalation",
            entity_id=task.id,
        )
        .one()
    )
    assert inbox_item.in_app_status == "delivered"
    assert inbox_item.email_status == "sent"
    assert inbox_item.email_attempts == 1
    assert [recipient for recipient, _, _ in outbox].count(maya.email) == 1

    second_pass = _run(db_session, DUE)

    assert second_pass.escalations_sent == 0
    assert db_session.query(TaskNotification).filter_by(
        task_id=task.id,
        kind="escalation",
        recipient_employee_id=maya.id,
    ).count() == 1
    assert db_session.query(UserNotification).filter_by(
        recipient_employee_id=maya.id,
        event_type="task_escalation",
        entity_id=task.id,
    ).count() == 1
    assert [recipient for recipient, _, _ in outbox].count(maya.email) == 1


def test_escalation_is_sent_only_once(db_session, rahul, maya, outbox):
    _task(db_session, rahul)
    for hours in (-24, -2, 0, 1, 24, 48, 72):
        _run(db_session, DUE + timedelta(hours=hours))
    assert sum(1 for to, _, _ in outbox if to == maya.email) == 1


def test_escalation_waits_for_two_missed_reminders(db_session, rahul, maya, outbox):
    """A late-created task only got the 2h reminder: escalate when the 1st overdue reminder is missed too."""
    _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=1))   # reminder_2h
    _run(db_session, DUE)                        # overdue1; only 1 missed so far
    assert not any(to == maya.email for to, _, _ in outbox)

    _run(db_session, DUE + timedelta(hours=24))  # overdue2; overdue1 now missed too
    assert sum(1 for to, _, _ in outbox if to == maya.email) == 1


def test_no_manager_skips_escalation_without_failing(db_session, outbox):
    loner = _employee(db_session, "Solo Person")
    _task(db_session, loner)
    _run(db_session, DUE - timedelta(hours=24))
    _run(db_session, DUE - timedelta(hours=2))
    summary = _run(db_session, DUE)
    assert summary.skipped_no_manager == 1
    assert summary.reminders_sent == 1
    assert all(to == loner.email for to, _, _ in outbox)


# --- Completed tasks ---------------------------------------------------------


def test_no_emails_for_completed_task(db_session, rahul, outbox):
    task = _task(db_session, rahul)
    task.status = "completed"
    db_session.commit()
    for hours in (-24, -2, 0, 24, 48):
        _run(db_session, DUE + timedelta(hours=hours))
    assert outbox == []
    assert _notifications(db_session, task) == []


def test_completing_stops_further_reminders_and_escalation(client, db_session, rahul, maya, outbox):
    task = _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=24))
    _run(db_session, DUE - timedelta(hours=2))

    response = client.patch(
        f"/api/v1/tasks/{task.id}/status",
        params={**EMPLOYEE, "actor_email": rahul.email},
        json={"status": "completed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "completed"
    assert response.json()["task"]["completed_at"] is not None

    for hours in (0, 24, 48):
        _run(db_session, DUE + timedelta(hours=hours))
    assert len(outbox) == 2  # only the two pre-due reminders
    assert not any(to == maya.email for to, _, _ in outbox)


def test_status_is_rechecked_right_before_sending(db_session, rahul, outbox, monkeypatch):
    """A task completed while a run is in progress gets no e-mail."""
    task = _task(db_session, rahul)
    from app import task_reminder_service

    original = task_reminder_service._is_open

    def completed_meanwhile(db, t):
        db.query(Task).filter(Task.id == t.id).update({"status": "completed"})
        db.commit()
        return original(db, t)

    monkeypatch.setattr(task_reminder_service, "_is_open", completed_meanwhile)
    _run(db_session, DUE - timedelta(hours=24))
    assert outbox == []
    assert _notifications(db_session, task) == []


# --- Duplicate prevention / retries -------------------------------------------


def test_failed_send_is_retried_but_never_duplicated(db_session, rahul, monkeypatch):
    task = _task(db_session, rahul)
    attempts: list[str] = []

    def flaky(recipient, subject, body):
        attempts.append(recipient)
        if len(attempts) == 1:
            raise OSError("SMTP unavailable")

    monkeypatch.setattr("app.task_service.send_email", flaky)
    first = _run(db_session, DUE - timedelta(hours=24))
    assert first.failed == 1
    assert [n.status for n in _notifications(db_session, task)] == ["failed"]

    _run(db_session, DUE - timedelta(hours=23))
    _run(db_session, DUE - timedelta(hours=22))
    assert len(attempts) == 2
    notifications = _notifications(db_session, task)
    assert [(n.kind, n.status, n.error) for n in notifications] == [("reminder_24h", "sent", None)]


def test_reminder_claimed_by_another_run_is_not_sent_again(db_session, rahul, outbox):
    """A 'pending' row means another process already claimed (and may be sending) it."""
    task = _task(db_session, rahul)
    db_session.add(TaskNotification(
        id=str(uuid.uuid4()), task_id=task.id, recipient_employee_id=rahul.id,
        kind="reminder_24h", subject="s", body="b", status="pending",
        idempotency_key=f"task-reminder:{task.id}:a1:d0:t24h",
    ))
    db_session.commit()
    _run(db_session, DUE - timedelta(hours=24))
    assert outbox == []


def test_reassignment_or_new_due_date_starts_a_new_cycle(db_session, rahul, maya, outbox):
    task = _task(db_session, rahul)
    _run(db_session, DUE - timedelta(hours=24))

    new_due = DUE + timedelta(days=2)
    task.due_at = new_due
    task.calendar_sequence = (task.calendar_sequence or 0) + 1
    db_session.commit()

    _run(db_session, new_due - timedelta(hours=24))
    assert [to for to, _, _ in outbox] == [rahul.email, rahul.email]
    keys = [n.idempotency_key for n in _notifications(db_session, task)]
    assert keys[0].endswith(":d0:t24h") and keys[1].endswith(":d1:t24h")


# --- API ----------------------------------------------------------------------


def test_status_change_permissions(client, db_session, rahul):
    task = _task(db_session, rahul)

    other = client.patch(
        f"/api/v1/tasks/{task.id}/status",
        params={**EMPLOYEE, "actor_email": "someone.else@northstar.example"},
        json={"status": "completed"},
    )
    assert other.status_code == 403

    manager = client.patch(f"/api/v1/tasks/{task.id}/status", params=MANAGER, json={"status": "completed"})
    assert manager.status_code == 200
    reopened = client.patch(f"/api/v1/tasks/{task.id}/status", params=MANAGER, json={"status": "pending"})
    assert reopened.json()["task"]["status"] == "pending"
    assert reopened.json()["task"]["completed_at"] is None


def test_manual_run_endpoint(client, db_session, rahul, outbox):
    _task(db_session, rahul, due_at=datetime.now(timezone.utc) + timedelta(hours=1))

    assert client.post("/api/v1/tasks/reminders/run", params=EMPLOYEE).status_code == 403
    response = client.post("/api/v1/tasks/reminders/run", params=MANAGER)
    assert response.status_code == 200
    assert response.json()["reminders_sent"] == 1
    assert client.post("/api/v1/tasks/reminders/run", params=MANAGER).json()["reminders_sent"] == 0


# --- Scheduler -----------------------------------------------------------------


@pytest.mark.parametrize(("value", "enabled"), [("true", True), ("1", True), ("false", False), ("off", False)])
def test_scheduler_enabled_flag(monkeypatch, value, enabled):
    monkeypatch.setenv("TASK_REMINDER_SCHEDULER_ENABLED", value)
    assert task_reminder_scheduler.scheduler_enabled() is enabled


def test_scheduler_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setenv("TASK_REMINDER_SCHEDULER_ENABLED", "false")
    assert task_reminder_scheduler.start_task_reminder_scheduler() is False


def test_scheduler_loop_enqueues_passes_until_stopped(monkeypatch):
    stop = threading.Event()
    enqueued: list[int] = []

    def fake_enqueue_once():
        enqueued.append(1)
        if len(enqueued) == 2:
            stop.set()
        return "workflow-run"

    monkeypatch.setattr(task_reminder_scheduler, "enqueue_once", fake_enqueue_once)
    monkeypatch.setattr(stop, "wait", lambda timeout=None: stop.is_set())
    task_reminder_scheduler._loop(stop, interval=30)
    assert len(enqueued) == 2
