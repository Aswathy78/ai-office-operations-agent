"""Story 5.5 (FR-28): task deadlines in the assignee's calendar, kept in sync."""
from datetime import datetime, timedelta, timezone
import itertools
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import task_calendar_scheduler
from app import task_calendar_sync_service as sync_service
from app.db.database import get_db
from app.meeting.calendar_provider import CalendarProvider, CalendarProviderError, LocalCalendarProvider
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    CALENDAR_SYNC_STATUSES,
    Base,
    CalendarDeadlineEntry,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    Task,
    TaskCalendarSync,
)

DUE = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
MANAGER = {"actor_id": "user-002", "actor_role": "manager", "actor_team": "Engineering"}


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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
    app.dependency_overrides.pop(get_db, None)


class FlakyProvider(LocalCalendarProvider):
    """The local provider with scripted failures and optional wrong read-backs."""

    def __init__(self, failures: int = 0, error: Exception | None = None):
        self.failures = failures
        self.error = error or CalendarProviderError("Calendar API unavailable (503)")
        self.writes = 0
        self.corrupt_read_back = False

    def upsert_deadline_entry(self, db, **kwargs):
        self.writes += 1
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return super().upsert_deadline_entry(db, **kwargs)

    def get_deadline_entry(self, db, entry_id):
        entry = super().get_deadline_entry(db, entry_id)
        if entry is not None and self.corrupt_read_back:
            entry.due_at = entry.due_at + timedelta(days=1)
        return entry


@pytest.fixture()
def provider(monkeypatch):
    instance = FlakyProvider()
    monkeypatch.setattr(sync_service, "get_calendar_provider", lambda: instance)
    return instance


def _employee(db: Session, name: str, team: str = "Engineering") -> Employee:
    employee = Employee(
        id=str(uuid.uuid4()), name=name, team=team, is_active=True,
        email=f"{name.lower().replace(' ', '.')}@northstar.example",
    )
    db.add(employee)
    db.commit()
    return employee


@pytest.fixture()
def rahul(db_session):
    return _employee(db_session, "Rahul Kumar")


def _task(db: Session, assignee: Employee, due_at: datetime = DUE, title="Prepare the release notes") -> Task:
    meeting_id, recording_id, transcript_id = (str(uuid.uuid4()) for _ in range(3))
    created = due_at - timedelta(days=5)
    db.add_all([
        Meeting(id=meeting_id, organizer_id="user-002", title="Sprint Planning", start_at=created,
                end_at=created + timedelta(hours=1), join_url=f"http://localhost/meetings/{meeting_id}",
                status="completed"),
        MeetingRecording(id=recording_id, meeting_id=meeting_id, status=RECORDING_STATUS_COMPLETED),
        MeetingTranscript(id=transcript_id, recording_id=recording_id, status=TRANSCRIPT_STATUS_COMPLETED,
                          language="en", transcript_text=title,
                          segments=[{"start": 0.0, "end": 2.0, "text": title, "speaker": "Maya"}],
                          completed_at=created),
    ])
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()), meeting_id=meeting_id, transcript_id=transcript_id,
        status=ACTION_ITEM_EXTRACTION_STATUS_COMPLETED, completed_at=created,
    )
    item = MeetingActionItem(
        id=str(uuid.uuid4()), owner_name=assignee.name, confirmed_employee_id=assignee.id,
        ownership_status="confirmed", task_description=title, deadline_at=due_at, deadline_text="Monday",
        priority="high", confidence=0.95, source_segment_index=0, source_start_seconds=0.0,
        source_end_seconds=2.0, source_quote=title, review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    extraction.items.append(item)
    task = Task(
        id=str(uuid.uuid4()), title=title, assignee_id=assignee.id, creator_id="user-002",
        source="meeting_action_item", source_action_item_id=item.id,
        idempotency_key=f"meeting-action-item:{item.id}", meeting_id=meeting_id, transcript_id=transcript_id,
        source_segment_index=0, source_timestamp_seconds=0.0, due_at=due_at, priority="high",
        status="pending", assignment_email_status="sent",
    )
    db.add_all([extraction, task])
    db.commit()
    return task


def _sync(db: Session, task: Task) -> TaskCalendarSync:
    db.expire_all()
    return db.query(TaskCalendarSync).filter_by(task_id=task.id).one()


def _entries(db: Session) -> list[CalendarDeadlineEntry]:
    db.expire_all()
    return db.query(CalendarDeadlineEntry).order_by(CalendarDeadlineEntry.created_at).all()


def _as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# --- Creating the calendar entry -----------------------------------------------------


def test_new_task_deadline_creates_calendar_entry_for_assignee(db_session, rahul, provider):
    task = _task(db_session, rahul)

    sync_service.reconcile(db_session)
    assert _sync(db_session, task).status == "pending"
    sync_service.process_due(db_session)

    sync = _sync(db_session, task)
    assert sync.status == "completed"
    assert sync.last_synced_at is not None
    [entry] = _entries(db_session)
    assert entry.owner_email == rahul.email
    assert entry.title == "Task due: Prepare the release notes"
    assert _as_utc(entry.due_at) == DUE
    assert entry.reminder_minutes_before == 60
    assert entry.external_ref == f"task:{task.id}"
    assert sync.external_entry_id == entry.id


def test_completed_tasks_get_no_calendar_entry(db_session, rahul, provider):
    task = _task(db_session, rahul)
    task.status = "completed"
    db_session.commit()
    sync_service.run_sync_pass(db_session)
    assert db_session.query(TaskCalendarSync).count() == 0
    assert _entries(db_session) == []


# --- Updating it ----------------------------------------------------------------------


def test_due_date_change_updates_the_existing_entry(client, db_session, rahul, provider):
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    [entry] = _entries(db_session)
    entry_id, entry_version = entry.id, entry.version
    new_due = DUE + timedelta(days=3)

    response = client.patch(f"/api/v1/tasks/{task.id}/due-date", params=MANAGER,
                            json={"due_at": new_due.isoformat(), "reason": "Scope grew"})
    assert response.status_code == 200, response.text
    assert _sync(db_session, task).status == "pending"  # queued straight away
    assert _sync(db_session, task).pending_reason == "Due date changed"

    sync_service.process_due(db_session)

    [updated] = _entries(db_session)
    assert updated.id == entry_id  # same entry, updated in place
    assert _as_utc(updated.due_at) == new_due
    assert updated.version == entry_version + 1
    assert _sync(db_session, task).status == "completed"


def test_reconcile_detects_due_date_changes_made_anywhere(db_session, rahul, provider):
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    task.due_at = DUE + timedelta(days=1)
    task.updated_at = datetime.now(timezone.utc)
    db_session.commit()

    sync_service.run_sync_pass(db_session)

    assert _as_utc(_entries(db_session)[0].due_at) == DUE + timedelta(days=1)


def test_reassignment_moves_the_entry_to_the_new_assignee(db_session, rahul, provider):
    ava = _employee(db_session, "Ava Patel")
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)

    task.assignee_id = ava.id
    task.updated_at = datetime.now(timezone.utc)
    db_session.commit()
    sync_service.run_sync_pass(db_session)

    old, new = _entries(db_session)
    assert old.owner_email == rahul.email and old.deleted_at is not None
    assert new.owner_email == ava.email and new.deleted_at is None
    assert _sync(db_session, task).status == "completed"


# --- External changes (provider polled every 5 minutes) ------------------------------------


@pytest.mark.parametrize("change", ["moved", "deleted", "renamed"])
def test_external_change_is_detected_and_task_deadline_restored(db_session, rahul, provider, change):
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    entry = _entries(db_session)[0]
    if change == "moved":
        entry.due_at = DUE + timedelta(days=7)
    elif change == "deleted":
        entry.deleted_at = datetime.now(timezone.utc)
    else:
        entry.title = "Something else"
    entry.version += 1
    db_session.commit()

    summary = sync_service.poll_external_changes(db_session)

    assert summary.external_changes == 1
    sync = _sync(db_session, task)
    assert sync.status == "pending"
    assert sync.external_change_detected_at is not None
    assert {"moved": "moved to 09 Nov 2026", "deleted": "deleted from the calendar",
            "renamed": "edited outside the app"}[change] in sync.external_change_detail

    sync_service.process_due(db_session)
    live = [e for e in _entries(db_session) if e.deleted_at is None]
    assert len(live) == 1
    assert _as_utc(live[0].due_at) == DUE  # the task is the source of truth
    assert live[0].title == "Task due: Prepare the release notes"
    assert _sync(db_session, task).status == "completed"


def test_poll_without_changes_keeps_completed(db_session, rahul, provider):
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    summary = sync_service.poll_external_changes(db_session)
    assert (summary.polled, summary.external_changes) == (1, 0)
    sync = _sync(db_session, task)
    assert sync.status == "completed" and sync.last_polled_at is not None


def test_scheduler_polls_provider_every_five_minutes(monkeypatch):
    assert task_calendar_scheduler.poll_interval_seconds() == 300
    clock = {"now": 0.0}
    passes: list[bool] = []
    stop = threading.Event()

    def fake_enqueue_once(*, poll):
        passes.append(poll)
        clock["now"] += 60  # each pass is one minute apart
        if len(passes) == 11:
            stop.set()
        return {}

    monkeypatch.setattr(task_calendar_scheduler, "enqueue_once", fake_enqueue_once)
    monkeypatch.setattr(task_calendar_scheduler.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(task_calendar_scheduler._wake_event, "wait", lambda timeout=None: stop.is_set())
    task_calendar_scheduler._loop(stop, sync_interval=60, poll_interval=300)

    assert [i for i, polled in enumerate(passes) if polled] == [0, 5, 10]


# --- Statuses, retries and failures ----------------------------------------------------------


def test_exactly_five_statuses_and_only_lifecycle_transitions():
    assert CALENDAR_SYNC_STATUSES == ("pending", "processing", "completed", "retrying", "failed")
    allowed = {
        ("pending", "processing"), ("processing", "completed"), ("processing", "retrying"),
        ("processing", "failed"), ("retrying", "processing"), ("retrying", "pending"),
        ("completed", "pending"), ("failed", "pending"),
    }
    for source, target in itertools.product(CALENDAR_SYNC_STATUSES, repeat=2):
        sync = TaskCalendarSync(status=source)
        if (source, target) in allowed:
            sync_service.transition(sync, target)
            assert sync.status == target
        else:
            with pytest.raises(sync_service.InvalidCalendarSyncTransition):
                sync_service.transition(sync, target)


def test_provider_error_retries_with_backoff_then_completes(db_session, rahul, provider):
    provider.failures = 1
    task = _task(db_session, rahul)

    sync_service.run_sync_pass(db_session)
    sync = _sync(db_session, task)
    assert sync.status == "retrying"
    assert "503" in sync.last_error
    wait = (_as_utc(sync.next_attempt_at) - datetime.now(timezone.utc)).total_seconds()
    assert 50 <= wait <= 61  # 1 minute

    sync_service.process_due(db_session)  # backoff not over: nothing happens
    assert _sync(db_session, task).status == "retrying"

    sync_service.process_due(db_session, now=datetime.now(timezone.utc) + timedelta(minutes=2))
    sync = _sync(db_session, task)
    assert sync.status == "completed" and sync.last_error is None and sync.attempt_count == 2


def test_backoff_schedule_is_1_5_15_minutes():
    assert [sync_service.backoff_seconds(n) for n in (1, 2, 3, 4)] == [60, 300, 900, 900]
    assert sync_service.max_attempts() == 5


def test_failure_after_five_attempts_is_never_completed(client, db_session, rahul, provider):
    provider.failures = 99
    task = _task(db_session, rahul)
    seen = set()
    now = datetime.now(timezone.utc)
    sync_service.reconcile(db_session)
    for _ in range(8):
        sync_service.process_due(db_session, now=now)
        seen.add(_sync(db_session, task).status)
        now += timedelta(hours=1)

    sync = _sync(db_session, task)
    assert sync.status == "failed"
    assert sync.attempt_count == 5
    assert "completed" not in seen
    assert provider.writes == 5
    shown = client.get(
        "/api/v1/tasks/",
        params={
            "assignee_email": rahul.email,
            "actor_id": rahul.id,
            "actor_role": "employee",
            "actor_email": rahul.email,
        },
    ).json()[0]["calendar_sync"]
    assert shown["status"] == "failed" and shown["last_error"]


def test_read_back_mismatch_is_not_completed(db_session, rahul, provider):
    provider.corrupt_read_back = True
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    sync = _sync(db_session, task)
    assert sync.status == "retrying"
    assert "could not be verified" in sync.last_error


def test_provider_without_deadline_support_reports_failure(db_session, rahul, monkeypatch):
    class MeetingsOnly(LocalCalendarProvider):
        upsert_deadline_entry = CalendarProvider.upsert_deadline_entry

    monkeypatch.setattr(sync_service, "get_calendar_provider", lambda: MeetingsOnly())
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    sync = _sync(db_session, task)
    assert sync.status == "retrying"
    assert "does not support task deadlines" in sync.last_error


def test_failed_sync_can_be_retried_by_assignee(client, db_session, rahul, provider):
    provider.failures = 99
    task = _task(db_session, rahul)
    sync_service.reconcile(db_session)
    now = datetime.now(timezone.utc)
    for _ in range(6):
        sync_service.process_due(db_session, now=now)
        now += timedelta(hours=1)
    assert _sync(db_session, task).status == "failed"
    provider.failures = 0

    someone_else = {"actor_id": "user-009", "actor_role": "employee", "actor_email": "other@northstar.example"}
    assert client.post(f"/api/v1/tasks/{task.id}/calendar-sync/retry", params=someone_else).status_code == 403

    assignee = {"actor_id": "user-001", "actor_role": "employee", "actor_email": rahul.email}
    response = client.post(f"/api/v1/tasks/{task.id}/calendar-sync/retry", params=assignee)
    assert response.status_code == 200
    assert response.json()["calendar_sync"]["status"] == "pending"

    sync_service.process_due(db_session)
    assert _sync(db_session, task).status == "completed"


def test_only_failed_syncs_can_be_retried(client, db_session, rahul, provider):
    task = _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    response = client.post(f"/api/v1/tasks/{task.id}/calendar-sync/retry", params=MANAGER)
    assert response.status_code == 409


def test_interrupted_processing_is_retried_not_completed(db_session, rahul, provider):
    task = _task(db_session, rahul)
    sync_service.reconcile(db_session)
    sync = _sync(db_session, task)
    sync.status = "processing"
    sync.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    assert sync_service.recover_stuck_processing(db_session) == 1
    assert _sync(db_session, task).status == "retrying"


def test_new_change_after_failure_starts_fresh_attempts(db_session, rahul, provider):
    provider.failures = 99
    task = _task(db_session, rahul)
    sync_service.reconcile(db_session)
    now = datetime.now(timezone.utc)
    for _ in range(6):
        sync_service.process_due(db_session, now=now)
        now += timedelta(hours=1)
    # Make it look previously synced, then change the due date after the failure.
    sync = _sync(db_session, task)
    sync.synced_due_at = DUE
    sync.status_changed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    task.due_at = DUE + timedelta(days=2)
    task.updated_at = datetime.now(timezone.utc)
    db_session.commit()
    provider.failures = 0

    sync_service.run_sync_pass(db_session)

    assert _sync(db_session, task).status == "completed"
    assert _sync(db_session, task).attempt_count == 1


# --- API ------------------------------------------------------------------------------------


def test_task_list_shows_exactly_one_calendar_status(client, db_session, rahul, provider):
    task = _task(db_session, rahul)
    before = client.get(
        "/api/v1/tasks/",
        params={
            "assignee_email": rahul.email,
            "actor_id": rahul.id,
            "actor_role": "employee",
            "actor_email": rahul.email,
        },
    ).json()[0]
    assert before["calendar_sync"] is None  # not picked up yet

    sync_service.run_sync_pass(db_session)
    after = client.get("/api/v1/tasks/dashboard", params=MANAGER).json()
    [shown] = [t for t in after if t["id"] == task.id]
    assert shown["calendar_sync"]["status"] == "completed"
    assert shown["calendar_sync"]["provider"] == "local"


def test_run_endpoint_requires_manager(client, db_session, rahul, provider):
    _task(db_session, rahul)
    employee = {"actor_id": "user-001", "actor_role": "employee"}
    assert client.post("/api/v1/tasks/calendar-sync/run", params=employee).status_code == 403
    result = client.post("/api/v1/tasks/calendar-sync/run", params=MANAGER).json()
    assert result["completed"] == 1


def test_calendar_times_are_sent_as_utc(client, db_session, rahul, provider):
    """SQLite drops the timezone; the API must still mark times as UTC so browsers show local time."""
    _task(db_session, rahul)
    sync_service.run_sync_pass(db_session)
    shown = client.get(
        "/api/v1/tasks/",
        params={
            "assignee_email": rahul.email,
            "actor_id": rahul.id,
            "actor_role": "employee",
            "actor_email": rahul.email,
        },
    ).json()[0]["calendar_sync"]
    synced = datetime.fromisoformat(shown["last_synced_at"].replace("Z", "+00:00"))
    assert synced.utcoffset() == timedelta(0)
    assert abs((synced - datetime.now(timezone.utc)).total_seconds()) < 60
