import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.db.database import get_db
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    NotificationPreference,
    Task,
    UserNotification,
    WorkflowRun,
    WorkflowStep,
)
from app.meeting.service import force_complete_meeting
from app.workflow_service import (
    MEETING_STEPS,
    claim_workflow,
    enqueue_calendar_monitor,
    enqueue_meeting_workflow,
    enqueue_task_reminders,
    enqueue_workflow,
    recover_stale_workflows,
    workflow_response,
)
from app.workflow_worker import _fail_step, _notify_meeting_result, process_workflow
from app.workflow_router import stream_meeting_workflow
from app import task_calendar_scheduler, task_reminder_scheduler


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


def _meeting(db: Session, *, status: str = "completed") -> tuple[Meeting, MeetingRecording]:
    meeting_id = str(uuid.uuid4())
    meeting = Meeting(
        id=meeting_id,
        organizer_id="organizer",
        title="Workflow acceptance test",
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status=status,
    )
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    db.add_all([meeting, recording])
    db.commit()
    return meeting, recording


def test_recording_and_meeting_end_triggers_are_idempotent(db_session: Session):
    meeting, _recording = _meeting(db_session, status="active")

    first = enqueue_meeting_workflow(db_session, meeting.id)
    duplicate = enqueue_meeting_workflow(db_session, meeting.id)

    assert first is not None
    assert duplicate is not None
    assert first.id == duplicate.id
    assert db_session.query(WorkflowRun).filter_by(meeting_id=meeting.id).count() == 1
    force_complete_meeting(db_session, meeting.id, organizer_id=meeting.organizer_id)
    assert enqueue_meeting_workflow(db_session, meeting.id).id == first.id
    assert [
        step["step_key"] for step in workflow_response(db_session, first)["steps"]
    ] == list(MEETING_STEPS)


@pytest.mark.parametrize(
    ("email_enabled", "expected_status"),
    [(True, "sent"), (False, "disabled")],
)
def test_workflow_result_email_obeys_recipient_preferences(
    db_session: Session,
    monkeypatch,
    email_enabled: bool,
    expected_status: str,
):
    meeting, _recording = _meeting(db_session)
    employee = Employee(
        id=meeting.organizer_id,
        name="Workflow Organizer",
        email="organizer@example.test",
        is_active=True,
    )
    db_session.add(employee)
    db_session.add(
        NotificationPreference(
            employee_id=employee.id,
            in_app_enabled=True,
            email_enabled=email_enabled,
        )
    )
    db_session.commit()
    run = enqueue_meeting_workflow(db_session, meeting.id)
    assert run is not None
    sent: list[str] = []
    monkeypatch.setattr(
        "app.task_service.send_email",
        lambda recipient, *_args: sent.append(recipient),
    )

    _notify_meeting_result(db_session, run, failed=False)

    notification = (
        db_session.query(UserNotification)
        .filter_by(idempotency_key=f"workflow-result:{run.id}:meeting_workflow_completed")
        .one()
    )
    assert notification.recipient_employee_id == employee.id
    assert notification.email_status == expected_status
    assert (sent == [employee.email]) is email_enabled


def test_queue_claim_uses_priority_and_global_concurrency_limit(
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setenv("WORKFLOW_CONCURRENCY", "1")
    meeting, _ = _meeting(db_session)
    meeting_run = enqueue_meeting_workflow(db_session, meeting.id)
    reminder_run = enqueue_task_reminders(db_session, interval_seconds=300)

    claimed = claim_workflow(db_session)
    assert claimed.id == meeting_run.id
    assert claimed.status == "processing"
    assert claim_workflow(db_session) is None

    claimed.status = "completed"
    claimed.claimed_at = None
    db_session.commit()
    assert claim_workflow(db_session).id == reminder_run.id


def test_simultaneous_claims_respect_concurrency_and_priority(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("WORKFLOW_CONCURRENCY", "2")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow-claims.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    priorities = (100, 20, 10, 0)
    with factory() as db:
        for index, priority in enumerate(priorities):
            enqueue_workflow(
                db,
                workflow_type=f"test_{index}",
                idempotency_key=f"simultaneous-claim-{index}",
                priority=priority,
                steps=("test_step",),
            )

    start = threading.Barrier(8)

    def claim_one():
        with factory() as db:
            start.wait(timeout=10)
            run = claim_workflow(db)
            return (run.id, run.priority) if run is not None else None

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(lambda _index: claim_one(), range(8)))

        claimed = [claim for claim in claims if claim is not None]
        with factory() as db:
            processing = (
                db.query(WorkflowRun)
                .filter(WorkflowRun.status == "processing")
                .all()
            )
            assert len(processing) == 2
            assert len({run.id for run in processing}) == 2
            assert {priority for _, priority in claimed} == {100, 20}
    finally:
        engine.dispose()


def test_concurrent_duplicate_enqueues_create_one_persisted_run(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow-idempotency.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    start = threading.Barrier(4)

    def enqueue_same_run(_index: int) -> str:
        with factory() as db:
            start.wait(timeout=10)
            run = enqueue_workflow(
                db,
                workflow_type="test_idempotency",
                idempotency_key="simultaneous-identical-trigger",
                steps=("one_step",),
            )
            return run.id

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            run_ids = list(executor.map(enqueue_same_run, range(4)))
        with factory() as db:
            assert len(set(run_ids)) == 1
            assert db.query(WorkflowRun).filter_by(
                idempotency_key="simultaneous-identical-trigger"
            ).count() == 1
            assert db.query(WorkflowStep).filter_by(
                workflow_run_id=run_ids[0]
            ).count() == 1
    finally:
        engine.dispose()


def test_reminder_and_calendar_schedulers_enqueue_shared_workflow_jobs(
    db_session: Session,
    monkeypatch,
):
    import app.db.database

    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, autocommit=False)
    monkeypatch.setattr(app.db.database, "SessionLocal", factory)
    monkeypatch.setattr(task_reminder_scheduler, "scheduler_interval_seconds", lambda: 60)
    monkeypatch.setattr(task_calendar_scheduler, "sync_interval_seconds", lambda: 15)

    reminder_id = task_reminder_scheduler.enqueue_once()
    calendar_id = task_calendar_scheduler.enqueue_once(poll=True)

    reminder = db_session.get(WorkflowRun, reminder_id)
    calendar = db_session.get(WorkflowRun, calendar_id)
    assert reminder is not None
    assert reminder.workflow_type == "task_reminders"
    assert reminder.priority == 20
    assert reminder.status == "pending"
    assert calendar is not None
    assert calendar.workflow_type == "calendar_monitor"
    assert calendar.priority == 10
    assert calendar.payload == {"poll": True}
    assert calendar.status == "pending"


def test_backend_startup_starts_both_workflow_schedulers(monkeypatch):
    import main
    import app.db.database
    from app import workflow_worker

    started: list[str] = []
    monkeypatch.setattr(main, "STORY_6_3_TEST_MODE", False)
    monkeypatch.setattr(main, "load_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr(main, "create_tables", lambda: None)
    monkeypatch.setattr(
        app.db.database,
        "SessionLocal",
        lambda: nullcontext(object()),
    )
    monkeypatch.setattr("app.db.seed_employees.seed_development_employees", lambda _db: None)
    monkeypatch.setattr("app.db.seed_documents.seed_company_documents", lambda _db: None)
    monkeypatch.setattr(main, "start_task_reminder_scheduler", lambda: started.append("reminders"))
    monkeypatch.setattr(main, "start_task_calendar_scheduler", lambda: started.append("calendar"))
    monkeypatch.setattr(main, "start_email_dispatcher", lambda: None)
    monkeypatch.setattr(main, "start_transcription_worker", lambda: None)
    monkeypatch.setattr(main, "start_document_processor", lambda: None)
    monkeypatch.setattr(workflow_worker, "start_workflow_workers", lambda: None)

    main.startup()

    assert started == ["reminders", "calendar"]


def test_workflow_state_and_checkpoints_survive_database_session_reload(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow-persistence.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        with factory() as db:
            run = enqueue_workflow(
                db,
                workflow_type="test_persistence",
                idempotency_key="persistent-workflow",
                steps=("persisted_step",),
            )
            run_id = run.id
            step = db.query(WorkflowStep).filter_by(workflow_run_id=run_id).one()
            step.status = "processing"
            run.status = "processing"
            db.commit()

        with factory() as db:
            run = db.get(WorkflowRun, run_id)
            step = db.query(WorkflowStep).filter_by(workflow_run_id=run_id).one()
            assert run.status == "processing"
            assert step.status == "processing"
            step.checkpoint = {"verified_record_id": "readback-123"}
            step.status = "completed"
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()

        with factory() as db:
            run = db.get(WorkflowRun, run_id)
            step = db.query(WorkflowStep).filter_by(workflow_run_id=run_id).one()
            assert run.status == "completed"
            assert run.completed_at is not None
            assert step.status == "completed"
            assert step.checkpoint == {"verified_record_id": "readback-123"}
    finally:
        engine.dispose()


def test_worker_startup_recovers_stale_jobs_and_shutdown_joins_worker(
    tmp_path,
    monkeypatch,
):
    from app import workflow_worker

    engine = create_engine(f"sqlite:///{tmp_path / 'workflow-worker-lifecycle.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        run = enqueue_workflow(
            db,
            workflow_type="test_recovery",
            idempotency_key="startup-stale-workflow",
            steps=("stuck_step",),
        )
        run_id = run.id
        step = db.query(WorkflowStep).filter_by(workflow_run_id=run_id).one()
        step_id = step.id
        run.status = "processing"
        run.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        step.status = "processing"
        db.commit()

    worker_started = threading.Event()
    monkeypatch.setattr(workflow_worker, "SessionLocal", factory)
    monkeypatch.setattr(workflow_worker, "worker_count", lambda: 1)
    monkeypatch.setattr(workflow_worker, "_threads", [])
    monkeypatch.setattr(workflow_worker, "_stop_event", None)

    def controlled_worker(stop: threading.Event, _worker_index: int) -> None:
        worker_started.set()
        stop.wait(timeout=5)

    monkeypatch.setattr(workflow_worker, "_worker_loop", controlled_worker)
    try:
        assert workflow_worker.start_workflow_workers() is True
        assert worker_started.wait(timeout=2)
        with factory() as db:
            assert db.get(WorkflowRun, run_id).status == "retrying"
            assert db.get(WorkflowStep, step_id).status == "retrying"
    finally:
        workflow_worker.stop_workflow_workers()
        engine.dispose()

    assert workflow_worker._threads == []


def test_failed_workflow_uses_exponential_backoff_then_stops_retrying(
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setattr("app.workflow_worker._retry_seconds", 5)
    run = enqueue_workflow(
        db_session,
        workflow_type="test_retry",
        idempotency_key="retry-backoff",
        steps=("test_step",),
        max_attempts=3,
    )
    step = db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id).one()

    for attempt, expected_delay in ((1, 5), (2, 10)):
        run.status = "processing"
        step.status = "processing"
        db_session.commit()
        _fail_step(db_session, run, step, RuntimeError("controlled transient failure"))
        db_session.expire_all()
        run = db_session.get(WorkflowRun, run.id)
        step = db_session.get(WorkflowStep, step.id)
        assert run.status == "retrying"
        assert run.attempt_count == attempt
        assert step.status == "retrying"
        assert (run.next_attempt_at - run.updated_at).total_seconds() == expected_delay

    run.status = "processing"
    step.status = "processing"
    db_session.commit()
    _fail_step(db_session, run, step, RuntimeError("controlled terminal failure"))
    db_session.expire_all()
    run = db_session.get(WorkflowRun, run.id)
    step = db_session.get(WorkflowStep, step.id)
    assert run.status == "failed"
    assert run.attempt_count == 3
    assert run.next_attempt_at is None
    assert run.completed_at is not None
    assert step.status == "failed"


def test_meeting_steps_checkpoint_only_after_persisted_readback(
    db_session: Session,
    monkeypatch,
):
    meeting, recording = _meeting(db_session)
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status="pending",
        language="en",
    )
    employee = Employee(
        id=meeting.organizer_id,
        name="Organizer",
        email="organizer@example.test",
        is_active=True,
    )
    db_session.add_all([transcript, employee])
    db_session.commit()
    run = enqueue_meeting_workflow(db_session, meeting.id)
    assert run is not None
    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)

    db_session.expire_all()
    steps = workflow_response(db_session, run)["steps"]
    assert steps[0]["status"] == "retrying"
    assert all(step["status"] == "pending" for step in steps[1:])

    transcript.status = "completed"
    transcript.transcript_text = "A completed meeting transcript."
    db_session.commit()

    def save_summary(db: Session, transcript_id: str):
        row = MeetingSummary(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=transcript_id,
            status="completed",
            overview="Verified summary.",
        )
        db.add(row)
        db.commit()
        return row

    def save_action_items(db: Session, transcript_id: str):
        row = MeetingActionItemExtraction(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=transcript_id,
            status=ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
        )
        db.add(row)
        db.flush()
        db.add(
            MeetingActionItem(
                id=str(uuid.uuid4()),
                extraction_id=row.id,
                confirmed_employee_id=employee.id,
                ownership_status=MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED,
                task_description="Prepare the follow-up report",
                deadline_at=datetime.now(timezone.utc) + timedelta(days=3),
                priority="medium",
                confidence=0.99,
                source_segment_index=0,
                source_start_seconds=0,
                source_end_seconds=4,
                review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
                reviewed_by=employee.id,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        return row

    monkeypatch.setattr("app.task_service.attempt_assignment_email", lambda *_args: None)
    monkeypatch.setattr("app.meeting.summary_service.generate_meeting_summary", save_summary)
    monkeypatch.setattr(
        "app.meeting.action_item_service.generate_meeting_action_items",
        save_action_items,
    )
    claimed.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)

    db_session.expire_all()
    completed = db_session.get(WorkflowRun, run.id)
    assert completed.status == "completed"
    completed_steps = workflow_response(db_session, completed)["steps"]
    assert all(step["status"] == "completed" for step in completed_steps)
    assert completed_steps[1]["checkpoint"]["summary_id"]
    assert completed_steps[2]["checkpoint"]["extraction_id"]
    task_ids = completed_steps[3]["checkpoint"]["task_ids"]
    assert len(task_ids) == 1
    assert db_session.get(Task, task_ids[0]).title == "Prepare the follow-up report"


def test_readback_failure_is_retried_and_then_reported_as_failed(
    db_session: Session,
    monkeypatch,
):
    meeting, recording = _meeting(db_session)
    employee = Employee(
        id=meeting.organizer_id,
        name="Organizer",
        email="organizer@example.test",
        is_active=True,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status="completed",
        transcript_text="Transcript.",
        language="en",
    )
    db_session.add_all([employee, transcript])
    db_session.commit()
    run = enqueue_meeting_workflow(db_session, meeting.id)
    run.max_attempts = 2
    db_session.commit()
    claimed = claim_workflow(db_session)
    monkeypatch.setattr(
        "app.meeting.summary_service.generate_meeting_summary",
        lambda *_args, **_kwargs: MeetingSummary(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=transcript.id,
            status="completed",
            overview="Not persisted.",
        ),
    )

    process_workflow(db_session, claimed.id)

    db_session.expire_all()
    retrying = db_session.get(WorkflowRun, run.id)
    retrying_steps = workflow_response(db_session, retrying)["steps"]
    assert retrying.status == "retrying"
    assert retrying.attempt_count == 1
    assert retrying_steps[0]["status"] == "completed"
    assert retrying_steps[1]["status"] == "retrying"

    retrying.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)

    db_session.expire_all()
    failed = db_session.get(WorkflowRun, run.id)
    assert failed.status == "failed"
    assert failed.attempt_count == 2
    failed_steps = workflow_response(db_session, failed)["steps"]
    assert failed_steps[0]["status"] == "completed"
    assert failed_steps[1]["status"] == "failed"
    assert "read-back verification" in failed_steps[1]["last_error"]


def test_stale_worker_lease_requeues_processing_job_and_step(db_session: Session):
    meeting, _ = _meeting(db_session)
    run = enqueue_meeting_workflow(db_session, meeting.id)
    step = db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id).first()
    now = datetime.now(timezone.utc)
    run.status = "processing"
    run.claimed_at = now - timedelta(hours=1)
    step.status = "processing"
    step.started_at = now - timedelta(hours=1)
    db_session.commit()

    assert recover_stale_workflows(db_session, stale_minutes=30) == 1

    db_session.expire_all()
    recovered_run = db_session.get(WorkflowRun, run.id)
    assert recovered_run.status == "retrying"
    assert recovered_run.attempt_count == 1
    assert db_session.get(WorkflowStep, step.id).status == "retrying"


def test_stale_workflow_at_retry_limit_becomes_terminal_failure(db_session: Session):
    meeting, _ = _meeting(db_session)
    run = enqueue_meeting_workflow(db_session, meeting.id)
    run.attempt_count = run.max_attempts - 1
    run.status = "processing"
    run.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    step = db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id).first()
    step.status = "processing"
    db_session.commit()

    assert recover_stale_workflows(db_session, stale_minutes=30) == 1

    db_session.expire_all()
    recovered_run = db_session.get(WorkflowRun, run.id)
    assert recovered_run.status == "failed"
    assert recovered_run.attempt_count == recovered_run.max_attempts
    assert recovered_run.claimed_at is None
    assert recovered_run.next_attempt_at is None
    assert db_session.get(WorkflowStep, step.id).status == "failed"


def test_reminders_and_due_date_monitors_execute_as_queued_workflows(
    db_session: Session,
    monkeypatch,
):
    reminder_calls: list[Session] = []
    calendar_calls: list[bool] = []

    class Summary:
        def as_dict(self):
            return {"checked": 0}

    monkeypatch.setattr(
        "app.task_reminder_service.run_task_reminders",
        lambda db: (reminder_calls.append(db) or Summary()),
    )
    monkeypatch.setattr(
        "app.task_calendar_sync_service.run_sync_pass",
        lambda _db, *, poll: (calendar_calls.append(poll) or Summary()),
    )
    reminder = enqueue_task_reminders(db_session, interval_seconds=300)
    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)
    calendar = enqueue_calendar_monitor(db_session, interval_seconds=15, poll=True)
    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)

    db_session.expire_all()
    assert reminder_calls
    assert calendar_calls == [True]
    assert db_session.get(WorkflowRun, reminder.id).status == "completed"
    assert db_session.get(WorkflowRun, calendar.id).status == "completed"


def test_completed_workflow_is_not_claimed_or_processed_again(
    db_session: Session,
    monkeypatch,
):
    calls: list[bool] = []

    class Summary:
        def as_dict(self):
            return {"completed": 1}

    monkeypatch.setattr(
        "app.task_calendar_sync_service.run_sync_pass",
        lambda _db, *, poll: (calls.append(poll) or Summary()),
    )
    run = enqueue_workflow(
        db_session,
        workflow_type="calendar_monitor",
        idempotency_key="completed-workflow-is-idempotent",
        payload={"poll": True},
        steps=("calendar_monitor",),
    )
    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)

    duplicate = enqueue_workflow(
        db_session,
        workflow_type="calendar_monitor",
        idempotency_key="completed-workflow-is-idempotent",
        payload={"poll": True},
        steps=("calendar_monitor",),
    )
    process_workflow(db_session, run.id)

    assert duplicate.id == run.id
    assert db_session.query(WorkflowRun).filter_by(idempotency_key="completed-workflow-is-idempotent").count() == 1
    assert db_session.get(WorkflowRun, run.id).status == "completed"
    assert claim_workflow(db_session) is None
    assert calls == [True]


def test_workflow_status_api_is_authenticated_and_meeting_scoped(
    client: TestClient,
    db_session: Session,
):
    meeting, _ = _meeting(db_session)
    run = enqueue_meeting_workflow(db_session, meeting.id)

    response = client.get(
        f"/api/v1/workflows/meetings/{meeting.id}",
        headers={"X-User-Id": meeting.organizer_id},
    )
    hidden = client.get(
        f"/api/v1/workflows/meetings/{meeting.id}",
        headers={"X-User-Id": "unrelated-user"},
    )

    assert response.status_code == 200
    assert response.json()["workflow"]["id"] == run.id
    assert response.json()["workflow"]["status"] == "pending"
    assert len(response.json()["workflow"]["steps"]) == len(MEETING_STEPS)
    assert hidden.status_code == 404


def test_workflow_events_stream_emits_persisted_status_changes(
    db_session: Session,
    monkeypatch,
):
    meeting, _ = _meeting(db_session)
    enqueue_meeting_workflow(db_session, meeting.id)
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, autocommit=False)

    class Request:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr("app.workflow_router.SessionLocal", factory)
    principal = AuthenticatedPrincipal(
        user_id=meeting.organizer_id,
        account_id="test-account",
        email="organizer@test.invalid",
        name="Organizer",
        role="employee",
        team=None,
        jti="test-session",
    )
    async def read_updates() -> tuple[str, str]:
        response = await stream_meeting_workflow(
            meeting.id,
            Request(),
            principal=principal,
            db=db_session,
        )
        events = response.body_iterator
        initial = await events.__anext__()
        with factory() as update_db:
            run = (
                update_db.query(WorkflowRun)
                .filter_by(meeting_id=meeting.id)
                .one()
            )
            run.status = "processing"
            run.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            update_db.commit()
        update_started = time.monotonic()
        updated = await events.__anext__()
        update_latency = time.monotonic() - update_started
        await events.aclose()
        return initial, updated, update_latency

    initial, updated, update_latency = asyncio.run(read_updates())
    assert "event: workflow" in initial
    assert '"status": "pending"' in initial
    assert "event: workflow" in updated
    assert '"status": "processing"' in updated
    assert update_latency <= 5
