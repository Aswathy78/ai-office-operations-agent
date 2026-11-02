"""Story 8.4 / FR-46 - automatic retry and dead-letter queue for workflow runs.

Acceptance criteria covered here:
  * Transient failures retry with exponential backoff and jitter, up to 5 times.
  * Exhausted jobs go to the dead-letter queue with status Failed, and a human is told.
  * Failed operations are shown as failed, never as successful.
  * An administrator can manually retry, cancel or resume a run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import workflow_service, workflow_worker
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AuthAccount,
    Base,
    Employee,
    Meeting,
    UserNotification,
    WorkflowDeadLetter,
    WorkflowEvent,
    WorkflowRun,
    WorkflowStep,
)
from app.workflow_service import (
    DeadLetterConflict,
    PermanentWorkflowError,
    backoff_delay_seconds,
    claim_workflow,
    dead_letter_run,
    enqueue_meeting_workflow,
    enqueue_workflow,
    recover_stale_workflows,
    resolve_dead_letter,
    workflow_response,
)
from app.workflow_worker import process_workflow


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No real e-mail, and a short, exact base delay for the backoff maths."""
    monkeypatch.setattr("app.notification_service.deliver_notification", lambda *a, **k: None)
    monkeypatch.setattr(workflow_worker, "_retry_seconds", 5)


def _person(db: Session, employee_id: str, role: str, *, enabled: bool = True, active: bool = True):
    db.add(Employee(
        id=employee_id, name=f"Person {employee_id}", email=f"{employee_id}@example.test",
        is_active=active,
    ))
    db.add(AuthAccount(
        id=f"acc-{employee_id}", employee_id=employee_id, email_normalized=f"{employee_id}@example.test",
        password_hash="x", role=role, is_enabled=enabled,
    ))
    db.commit()


@pytest.fixture()
def admin(db_session: Session) -> str:
    _person(db_session, "admin-1", "admin")
    return "admin-1"


def _principal(user_id: str, role: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user_id, account_id=f"acc-{user_id}", email=f"{user_id}@example.test",
        name=user_id, role=role, team=None, jti=f"jti-{user_id}",
    )


@pytest.fixture()
def client(db_session: Session):
    current = {"principal": _principal("admin-1", "admin")}
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: current["principal"]
    with TestClient(app) as test_client:
        test_client.login = lambda user_id, role: current.update(principal=_principal(user_id, role))
        yield test_client
    app.dependency_overrides.clear()


def _run(db: Session, *steps: str, key: str | None = None, payload: dict | None = None,
         workflow_type: str = "test_dlq", **extra) -> WorkflowRun:
    return enqueue_workflow(
        db, workflow_type=workflow_type, idempotency_key=key or str(uuid.uuid4()),
        steps=steps or ("only_step",), payload=payload, **extra,
    )


class Script:
    """Stands in for the real step work: per-step behaviour and a call log."""

    def __init__(self, monkeypatch, behaviour):
        self.calls: list[str] = []
        self.behaviour = behaviour

        def verify(db, run, step):
            self.calls.append(step.step_key)
            outcome = self.behaviour(step.step_key, self.calls.count(step.step_key))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome if isinstance(outcome, dict) else {"ok": step.step_key}

        monkeypatch.setattr(workflow_worker, "_verify_step", verify)


def _drain(db: Session, *, rounds: int = 30) -> int:
    """Claim and process due runs like the workers do, without waiting out the backoff."""
    processed = 0
    for _ in range(rounds):
        for run in db.query(WorkflowRun).filter(WorkflowRun.status.in_(("pending", "retrying"))):
            run.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        claimed = claim_workflow(db)
        if claimed is None:
            break
        process_workflow(db, claimed.id)
        processed += 1
    db.expire_all()
    return processed


def _utc(value: datetime) -> datetime:
    """SQLite hands back some timestamps naive and some aware; compare them as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _events(db: Session, run_id: str, event_type: str | None = None) -> list[WorkflowEvent]:
    query = db.query(WorkflowEvent).filter(WorkflowEvent.workflow_run_id == run_id)
    if event_type:
        query = query.filter(WorkflowEvent.event_type == event_type)
    return query.order_by(WorkflowEvent.sequence).all()


def _fail_always(monkeypatch, error: Exception | None = None) -> Script:
    return Script(monkeypatch, lambda step, n: error or RuntimeError("provider timed out"))


def _dead_letter_a_run(db: Session, monkeypatch, *steps: str, **kwargs) -> WorkflowRun:
    run = _run(db, *steps, **kwargs)
    _fail_always(monkeypatch)
    _drain(db)
    db.expire_all()
    return db.get(WorkflowRun, run.id)


# ---------------------------------------------------------------------------
# Backoff and jitter
# ---------------------------------------------------------------------------


def test_backoff_doubles_each_attempt_and_is_capped(monkeypatch):
    monkeypatch.setattr(workflow_service, "_jitter_factor", lambda: 1.0)

    assert [backoff_delay_seconds(n, 5) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff_delay_seconds(30, 5) == workflow_service.WORKFLOW_BACKOFF_CAP_SECONDS


def test_jitter_is_random_and_stays_within_its_bounds():
    delays = [backoff_delay_seconds(3, 5) for _ in range(300)]  # exponential delay is 20

    assert len(set(delays)) > 50, "the delay must vary from call to call"
    low = 20 * (1 - workflow_service.WORKFLOW_JITTER_RATIO)
    assert all(low <= delay <= 20 for delay in delays)


def test_jitter_never_makes_a_later_retry_wait_less_than_an_earlier_one():
    second = [backoff_delay_seconds(2, 5) for _ in range(300)]
    third = [backoff_delay_seconds(3, 5) for _ in range(300)]

    assert max(second) <= min(third)


def test_a_failed_attempt_schedules_a_jittered_exponential_retry(db_session, monkeypatch):
    monkeypatch.setattr(workflow_service, "_jitter_factor", lambda: 0.5)
    run = _run(db_session)
    _fail_always(monkeypatch)
    _drain(db_session, rounds=1)

    db_session.expire_all()
    run = db_session.get(WorkflowRun, run.id)
    assert run.status == "retrying" and run.attempt_count == 1
    assert (_utc(run.next_attempt_at) - _utc(run.updated_at)).total_seconds() == pytest.approx(2.5)
    [event] = _events(db_session, run.id, "retry_scheduled")
    assert event.retry_delay_seconds == pytest.approx(2.5)
    assert event.attempt_number == 1 and "timed out" in event.error


# ---------------------------------------------------------------------------
# Transient failures and the retry limit
# ---------------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_then_succeeds(db_session, admin, monkeypatch):
    run = _run(db_session, "a", "b")
    script = Script(monkeypatch, lambda step, n: RuntimeError("blip") if step == "b" and n < 3 else {})

    _drain(db_session)

    run = db_session.get(WorkflowRun, run.id)
    assert run.status == "completed" and run.last_error is None
    assert script.calls == ["a", "b", "b", "b"]
    assert [e.event_type for e in _events(db_session, run.id)].count("retry_scheduled") == 2
    assert db_session.query(WorkflowDeadLetter).count() == 0
    assert db_session.query(UserNotification).count() == 0, "nobody is paged for a blip"


def test_a_run_gets_exactly_five_attempts_then_goes_to_the_dead_letter_queue(db_session, admin, monkeypatch):
    run = _run(db_session)
    script = _fail_always(monkeypatch)

    _drain(db_session)

    run = db_session.get(WorkflowRun, run.id)
    assert len(script.calls) == 5
    assert run.status == "failed" and run.attempt_count == 5 and run.next_attempt_at is None
    kinds = [event.event_type for event in _events(db_session, run.id)]
    assert kinds.count("retry_scheduled") == 4 and kinds[-1] == "dead_lettered"
    [entry] = db_session.query(WorkflowDeadLetter).all()
    assert entry.status == "open" and entry.attempt_count == 5


def test_a_run_never_waits_more_than_five_attempts_even_if_asked_to(db_session):
    assert _run(db_session, max_attempts=50).max_attempts == workflow_service.WORKFLOW_MAX_ATTEMPTS == 5


def test_each_retry_waits_longer_than_the_one_before(db_session, monkeypatch):
    monkeypatch.setattr(workflow_service, "_jitter_factor", lambda: 1.0)
    run = _run(db_session)
    _fail_always(monkeypatch)

    _drain(db_session)

    delays = [e.retry_delay_seconds for e in _events(db_session, run.id, "retry_scheduled")]
    assert delays == [5, 10, 20, 40]


def test_a_permanent_failure_skips_the_retries(db_session, admin, monkeypatch):
    run = _run(db_session)
    script = _fail_always(monkeypatch, PermanentWorkflowError("The workflow meeting no longer exists."))

    _drain(db_session)

    run = db_session.get(WorkflowRun, run.id)
    assert len(script.calls) == 1
    assert run.status == "failed" and run.attempt_count == 1
    [entry] = db_session.query(WorkflowDeadLetter).all()
    assert entry.error_message == "The workflow meeting no longer exists."
    assert "Permanent failure" in _events(db_session, run.id, "dead_lettered")[0].message
    assert _events(db_session, run.id, "retry_scheduled") == []


def test_an_unsupported_step_is_a_permanent_failure(db_session, admin):
    run = _run(db_session, "no_such_step", workflow_type=workflow_service.MEETING_WORKFLOW)

    _drain(db_session)

    run = db_session.get(WorkflowRun, run.id)
    assert run.status == "failed" and run.attempt_count == 1
    assert "Unsupported" in db_session.query(WorkflowDeadLetter).one().error_message


# ---------------------------------------------------------------------------
# Dead-letter queue contents and failure status accuracy
# ---------------------------------------------------------------------------


def test_the_dead_letter_entry_keeps_what_is_needed_to_troubleshoot(db_session, admin, monkeypatch):
    run = _run(db_session, "fetch", "send", payload={"poll": True, "batch": 7}, key="dlq-details")
    Script(monkeypatch, lambda step, n: {} if step == "fetch" else RuntimeError(f"SMTP refused (try {n})"))

    _drain(db_session)

    [entry] = db_session.query(WorkflowDeadLetter).all()
    assert (entry.workflow_run_id, entry.workflow_type) == (run.id, "test_dlq")
    assert entry.failed_step_key == "send"
    assert entry.error_message == "SMTP refused (try 5)"
    assert (entry.attempt_count, entry.max_attempts) == (5, 5)
    assert entry.payload == {"poll": True, "batch": 7}
    history = [(e.event_type, e.attempt_number, e.error) for e in _events(db_session, run.id)
               if e.event_type == "retry_scheduled"]
    assert [error for _, _, error in history] == [f"SMTP refused (try {n})" for n in (1, 2, 3, 4)]
    assert [number for _, number, _ in history] == [1, 2, 3, 4]


def test_a_permanently_failed_run_is_never_shown_as_completed(db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch, "a", "b")

    state = workflow_response(db_session, run)
    assert state["status"] == "failed" and state["status"] != "completed"
    assert [step["status"] for step in state["steps"]] == ["failed", "pending"]
    assert state["attempt_count"] == 5 and state["max_attempts"] == 5
    assert "provider timed out" in state["last_error"]
    # Nothing picks it up again, and processing it is a no-op.
    assert claim_workflow(db_session) is None
    process_workflow(db_session, run.id)
    assert db_session.get(WorkflowRun, run.id).status == "failed"


def test_a_run_whose_early_steps_passed_still_ends_failed_not_completed(db_session, admin, monkeypatch):
    run = _run(db_session, "a", "b", "c")
    Script(monkeypatch, lambda step, n: RuntimeError("down") if step == "b" else {})

    _drain(db_session)

    run = db_session.get(WorkflowRun, run.id)
    steps = {s.step_key: s.status for s in db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id)}
    assert run.status == "failed" and steps == {"a": "completed", "b": "failed", "c": "pending"}


def test_a_failed_meeting_workflow_notifies_the_organizer_of_failure_not_success(db_session, admin, monkeypatch):
    _person(db_session, "organizer", "employee")
    meeting = Meeting(
        id=str(uuid.uuid4()), organizer_id="organizer", title="Quarterly review",
        start_at=datetime.now(timezone.utc) - timedelta(hours=1), end_at=datetime.now(timezone.utc),
        join_url="http://localhost/m", status="completed",
    )
    db_session.add(meeting)
    db_session.commit()
    run = _run(db_session, "transcription", workflow_type=workflow_service.MEETING_WORKFLOW,
               meeting_id=meeting.id)
    _fail_always(monkeypatch)

    _drain(db_session)

    kinds = {n.event_type for n in db_session.query(UserNotification).filter_by(recipient_employee_id="organizer")}
    assert kinds == {"meeting_workflow_failed"}
    assert db_session.get(WorkflowRun, run.id).status == "failed"


# ---------------------------------------------------------------------------
# Administrator notification
# ---------------------------------------------------------------------------


def test_dead_lettering_tells_every_active_administrator_once(db_session, monkeypatch, caplog):
    _person(db_session, "admin-1", "admin")
    _person(db_session, "admin-2", "admin")
    _person(db_session, "admin-off", "admin", enabled=False)
    _person(db_session, "admin-gone", "admin", active=False)
    _person(db_session, "hr-1", "hr_operations")
    run = _run(db_session, key="notify-admins")
    _fail_always(monkeypatch)

    _drain(db_session)
    dead_letter_run(db_session, run.id, step_key="only_step", error_message="again")  # a repeat call

    notices = db_session.query(UserNotification).filter_by(event_type="workflow_failure").all()
    assert sorted(n.recipient_employee_id for n in notices) == ["admin-1", "admin-2"]
    assert all(n.entity_type == "workflow_run" and n.entity_id == run.id for n in notices)
    assert all(n.entity_url == "/admin/workflows" for n in notices)
    assert all("dead-letter queue" in n.summary and "provider timed out" in n.summary for n in notices)
    entry = db_session.query(WorkflowDeadLetter).one()
    assert entry.notified_count == 2


def test_the_hr_operations_team_is_told_when_there_is_no_administrator(db_session, monkeypatch):
    _person(db_session, "hr-1", "hr_operations")
    _dead_letter_a_run(db_session, monkeypatch)

    recipients = [n.recipient_employee_id for n in db_session.query(UserNotification)]
    assert recipients == ["hr-1"]


def test_nobody_to_notify_is_logged_but_the_failure_is_still_recorded(db_session, monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        run = _dead_letter_a_run(db_session, monkeypatch)

    assert run.status == "failed"
    entry = db_session.query(WorkflowDeadLetter).one()
    assert entry.status == "open" and entry.notified_count == 0
    assert "no administrator can be notified" in caplog.text


def test_a_notification_error_never_hides_the_dead_letter_entry(db_session, admin, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr("app.notification_service.notify_workflow_failure", broken)

    run = _dead_letter_a_run(db_session, monkeypatch)

    assert run.status == "failed"
    entry = db_session.query(WorkflowDeadLetter).one()
    assert entry.status == "open" and entry.notified_count == 0


def test_a_stale_run_at_its_retry_limit_is_dead_lettered_and_administrators_are_told(db_session, admin):
    run = _run(db_session, "a")
    step = db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id).one()
    run.status, step.status = "processing", "processing"
    run.attempt_count = run.max_attempts - 1
    run.claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.commit()

    assert recover_stale_workflows(db_session) == 1

    run = db_session.get(WorkflowRun, run.id)
    assert run.status == "failed"
    entry = db_session.query(WorkflowDeadLetter).one()
    assert entry.failed_step_key == "a" and "lease expired" in entry.error_message
    assert [e.event_type for e in _events(db_session, run.id)] == ["lease_expired", "dead_lettered"]
    assert db_session.query(UserNotification).filter_by(recipient_employee_id=admin).count() == 1


# ---------------------------------------------------------------------------
# Administrator API: list, detail, retry, resume, cancel
# ---------------------------------------------------------------------------


def test_only_administrators_may_use_the_dead_letter_queue(client, db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch)
    for role in ("employee", "manager", "hr_operations"):
        client.login("someone", role)
        assert client.get("/api/v1/workflows/dead-letter").status_code == 403
        assert client.get(f"/api/v1/workflows/dead-letter/{run.id}").status_code == 403
        for action in ("retry", "resume", "cancel"):
            assert client.post(f"/api/v1/workflows/dead-letter/{run.id}/{action}").status_code == 403
    assert db_session.get(WorkflowRun, run.id).status == "failed"


def test_the_queue_lists_open_entries_with_counts(client, db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch, key="listed")

    body = client.get("/api/v1/workflows/dead-letter").json()

    [entry] = body["entries"]
    assert entry["run_id"] == run.id and entry["status"] == "open" and entry["run_status"] == "failed"
    assert entry["workflow_label"] == "test_dlq" and entry["failed_step_key"] == "only_step"
    assert entry["attempt_count"] == 5 and "provider timed out" in entry["error_message"]
    assert body["counts"] == {"open": 1, "retried": 0, "resumed": 0, "cancelled": 0}
    assert client.get("/api/v1/workflows/dead-letter?status=retried").json()["entries"] == []
    assert len(client.get("/api/v1/workflows/dead-letter?status=all").json()["entries"]) == 1
    assert client.get("/api/v1/workflows/dead-letter?status=bogus").status_code == 422


def test_the_detail_view_shows_steps_payload_and_history(client, db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch, "a", payload={"poll": False})

    detail = client.get(f"/api/v1/workflows/dead-letter/{run.id}").json()

    assert detail["workflow"]["status"] == "failed" and detail["payload"] == {"poll": False}
    assert detail["workflow"]["steps"][0]["last_error"] == "provider timed out"
    assert [e["event_type"] for e in detail["events"]].count("retry_scheduled") == 4
    assert detail["events"][-1]["event_type"] == "dead_lettered"
    assert detail["dead_letters"][0]["status"] == "open"
    assert client.get("/api/v1/workflows/dead-letter/not-a-run").status_code == 404


def _completed_then_failed(db_session, monkeypatch) -> tuple[WorkflowRun, Script]:
    """Step a completes with a checkpoint; step b fails five times; c never starts."""
    run = _run(db_session, "a", "b", "c", key=f"resolve-{uuid.uuid4()}")
    state = {"b_fails": True}
    script = Script(
        monkeypatch,
        lambda step, n: RuntimeError("downstream down") if step == "b" and state["b_fails"] else {"done": step},
    )
    script.state = state
    _drain(db_session)
    db_session.expire_all()
    run = db_session.get(WorkflowRun, run.id)
    assert run.status == "failed"
    return run, script


def test_retry_runs_the_workflow_again_from_the_first_step(client, db_session, admin, monkeypatch):
    run, script = _completed_then_failed(db_session, monkeypatch)
    script.state["b_fails"] = False
    script.calls.clear()

    response = client.post(f"/api/v1/workflows/dead-letter/{run.id}/retry", json={"note": "provider is back"})

    assert response.status_code == 200
    body = response.json()
    assert body["workflow"]["status"] == "pending" and body["workflow"]["attempt_count"] == 0
    assert [s["status"] for s in body["workflow"]["steps"]] == ["pending"] * 3
    assert all(s["checkpoint"] is None for s in body["workflow"]["steps"])
    [entry] = body["dead_letters"]
    assert entry["status"] == "retried" and entry["resolved_by"] == "admin-1"
    assert entry["resolution_note"] == "provider is back"

    _drain(db_session)
    assert script.calls == ["a", "b", "c"], "a retry starts over"
    assert db_session.get(WorkflowRun, run.id).status == "completed"
    assert any(e.event_type == "manual_retry" and e.actor_id == "admin-1" for e in _events(db_session, run.id))


def test_resume_continues_from_the_last_checkpoint(client, db_session, admin, monkeypatch):
    run, script = _completed_then_failed(db_session, monkeypatch)
    script.state["b_fails"] = False
    script.calls.clear()
    before = {s.step_key: s.checkpoint for s in db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id)}

    response = client.post(f"/api/v1/workflows/dead-letter/{run.id}/resume")

    assert response.status_code == 200
    steps = response.json()["workflow"]["steps"]
    assert [s["status"] for s in steps] == ["completed", "pending", "pending"]
    assert steps[0]["checkpoint"] == before["a"] == {"done": "a"}
    assert response.json()["dead_letters"][0]["status"] == "resumed"

    _drain(db_session)
    assert script.calls == ["b", "c"], "resume does not redo the completed step"
    assert db_session.get(WorkflowRun, run.id).status == "completed"


def test_cancel_ends_the_run_as_cancelled_not_failed_or_completed(client, db_session, admin, monkeypatch):
    run, script = _completed_then_failed(db_session, monkeypatch)
    script.calls.clear()

    response = client.post(f"/api/v1/workflows/dead-letter/{run.id}/cancel", json={"note": "obsolete"})

    assert response.status_code == 200
    body = response.json()
    assert body["workflow"]["status"] == "cancelled"
    assert body["dead_letters"][0]["status"] == "cancelled"
    assert body["dead_letters"][0]["resolution_note"] == "obsolete"
    assert claim_workflow(db_session) is None
    _drain(db_session)
    assert script.calls == [], "a cancelled run is never executed"
    assert db_session.get(WorkflowRun, run.id).status == "cancelled"
    assert client.get("/api/v1/workflows/dead-letter").json()["entries"] == []
    assert client.get("/api/v1/workflows/dead-letter?status=cancelled").json()["counts"]["cancelled"] == 1


def test_only_a_failed_run_can_be_acted_on(client, db_session, admin, monkeypatch):
    waiting = _run(db_session, key="still-waiting")
    for action in ("retry", "resume", "cancel"):
        response = client.post(f"/api/v1/workflows/dead-letter/{waiting.id}/{action}")
        assert response.status_code == 409
        assert client.post(f"/api/v1/workflows/dead-letter/missing/{action}").status_code == 404
    assert db_session.get(WorkflowRun, waiting.id).status == "pending"


def test_a_second_click_does_not_start_the_run_twice(client, db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch)

    first = client.post(f"/api/v1/workflows/dead-letter/{run.id}/retry")
    second = client.post(f"/api/v1/workflows/dead-letter/{run.id}/retry")
    third = client.post(f"/api/v1/workflows/dead-letter/{run.id}/cancel")

    assert (first.status_code, second.status_code, third.status_code) == (200, 409, 409)
    assert db_session.query(WorkflowRun).count() == 1
    assert db_session.get(WorkflowRun, run.id).status == "pending"
    assert [e.event_type for e in _events(db_session, run.id)].count("manual_retry") == 1


def test_two_administrators_acting_at_once_cannot_both_win(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("app.notification_service.deliver_notification", lambda *a, **k: None)
    try:
        with factory() as seed:
            run_id = _run(seed, key="race").id
            seed.query(WorkflowRun).update({"status": "failed", "attempt_count": 5})
            seed.commit()
            dead_letter_run(seed, run_id, step_key="only_step", error_message="down")
        first, second = factory(), factory()
        stale_view = second.get(WorkflowRun, run_id)  # loaded while it was still failed
        assert stale_view.status == "failed"

        resolve_dead_letter(first, run_id, action="retry", actor_id="admin-1")
        with pytest.raises(DeadLetterConflict):
            resolve_dead_letter(second, run_id, action="cancel", actor_id="admin-2")

        with factory() as check:
            assert check.get(WorkflowRun, run_id).status == "pending"
            assert check.query(WorkflowDeadLetter).one().status == "retried"
        first.close()
        second.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# No duplicates, and history survives
# ---------------------------------------------------------------------------


def test_a_manual_retry_reuses_the_run_instead_of_creating_a_second_job(client, db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch, key="meeting-follow-up:abc")

    client.post(f"/api/v1/workflows/dead-letter/{run.id}/retry")
    again = _run(db_session, key="meeting-follow-up:abc")  # the same trigger fires again

    assert again.id == run.id
    assert db_session.query(WorkflowRun).count() == 1
    assert db_session.query(WorkflowStep).count() == 1


def test_a_meeting_that_ends_again_does_not_queue_a_second_workflow_for_a_failed_run(db_session, admin, monkeypatch):
    _person(db_session, "organizer", "employee")
    from app.meeting.models import MeetingRecording

    meeting = Meeting(
        id=str(uuid.uuid4()), organizer_id="organizer", title="Retro",
        start_at=datetime.now(timezone.utc) - timedelta(hours=1), end_at=datetime.now(timezone.utc),
        join_url="http://localhost/m", status="completed",
    )
    db_session.add_all([meeting, MeetingRecording(
        id=str(uuid.uuid4()), meeting_id=meeting.id, status="completed",
        completed_at=datetime.now(timezone.utc),
    )])
    db_session.commit()
    first = enqueue_meeting_workflow(db_session, meeting.id)
    _fail_always(monkeypatch)
    _drain(db_session)

    second = enqueue_meeting_workflow(db_session, meeting.id)

    assert second.id == first.id and db_session.query(WorkflowRun).count() == 1
    assert db_session.get(WorkflowRun, first.id).status == "failed"


def test_history_survives_a_retry_and_a_second_failure_makes_a_second_entry(client, db_session, admin, monkeypatch):
    run = _dead_letter_a_run(db_session, monkeypatch)
    client.post(f"/api/v1/workflows/dead-letter/{run.id}/retry")

    _drain(db_session)  # still failing

    db_session.expire_all()
    assert db_session.get(WorkflowRun, run.id).status == "failed"
    entries = db_session.query(WorkflowDeadLetter).order_by(WorkflowDeadLetter.created_at).all()
    assert [entry.status for entry in entries] == ["retried", "open"]
    assert entries[1].attempt_count == 5, "the attempt budget was fresh after the manual retry"
    kinds = [e.event_type for e in _events(db_session, run.id)]
    assert kinds.count("dead_lettered") == 2 and kinds.count("manual_retry") == 1
    assert kinds.count("retry_scheduled") == 8
    # The administrator is told again about the second failure, but only once for it.
    assert db_session.query(UserNotification).filter_by(recipient_employee_id=admin).count() == 2


def test_recurring_reminder_and_calendar_runs_use_the_same_queue(db_session, admin, monkeypatch):
    from app.workflow_service import enqueue_calendar_monitor, enqueue_task_reminders

    reminders = enqueue_task_reminders(db_session, interval_seconds=60)
    calendar = enqueue_calendar_monitor(db_session, interval_seconds=60, poll=False)
    _fail_always(monkeypatch)

    _drain(db_session)

    assert {run.status for run in (db_session.get(WorkflowRun, reminders.id),
                                   db_session.get(WorkflowRun, calendar.id))} == {"failed"}
    assert {e.workflow_type for e in db_session.query(WorkflowDeadLetter)} == {
        "task_reminders", "calendar_monitor",
    }


# ---------------------------------------------------------------------------
# Migration 20261102_02
# ---------------------------------------------------------------------------


def _alembic(tmp_path):
    from pathlib import Path

    from alembic.config import Config

    path = tmp_path / "migrate.sqlite"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config, path


def _insert_run(connection, run_id: str, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.exec_driver_sql(
        "INSERT INTO workflow_runs (id, workflow_type, idempotency_key, priority, status, payload, "
        "attempt_count, max_attempts, last_error, created_at, updated_at) "
        "VALUES (?, 'task_reminders', ?, 0, ?, '{\"poll\": true}', 5, 5, 'old failure', ?, ?)",
        (run_id, f"key-{run_id}", status, now, now),
    )


def test_migration_backfills_runs_that_failed_before_the_queue_and_allows_cancelled(tmp_path):
    from alembic import command

    config, path = _alembic(tmp_path)
    command.upgrade(config, "20261102_01")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            _insert_run(connection, "old-failed", "failed")
            _insert_run(connection, "old-done", "completed")
            connection.exec_driver_sql(
                "INSERT INTO workflow_steps (id, workflow_run_id, step_key, step_order, status, "
                "attempt_count, updated_at) VALUES ('s1', 'old-failed', 'task_reminders', 0, 'failed', 5, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
        command.upgrade(config, "head")

        with engine.begin() as connection:
            rows = connection.exec_driver_sql(
                "SELECT workflow_run_id, status, failed_step_key, error_message, attempt_count "
                "FROM workflow_dead_letters"
            ).fetchall()
            assert rows == [("old-failed", "open", "task_reminders", "old failure", 5)]
            # A run can now be cancelled; an unknown status is still refused.
            _insert_run(connection, "to-cancel", "cancelled")
        with pytest.raises(Exception):
            with engine.begin() as connection:
                _insert_run(connection, "bad", "exploded")
    finally:
        engine.dispose()


def test_migration_downgrade_turns_cancelled_back_into_failed_and_drops_the_tables(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    config, path = _alembic(tmp_path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            _insert_run(connection, "cancelled-run", "cancelled")
        command.downgrade(config, "20261102_01")

        with engine.begin() as connection:
            assert connection.exec_driver_sql(
                "SELECT status FROM workflow_runs WHERE id = 'cancelled-run'"
            ).scalar_one() == "failed"
        tables = set(inspect(engine).get_table_names())
        assert "workflow_dead_letters" not in tables and "workflow_events" not in tables
        assert "workflow_runs" in tables
    finally:
        engine.dispose()


def test_migration_keeps_the_queue_indexes_and_the_idempotency_key(tmp_path):
    from alembic import command

    config, path = _alembic(tmp_path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            indexes = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name IN "
                    "('workflow_runs', 'workflow_dead_letters', 'workflow_events')"
                )
            }
            assert {
                "ix_workflow_runs_queue",
                "ix_workflow_runs_meeting",
                "uq_workflow_dead_letters_open_run",
                "ix_workflow_dead_letters_status",
                "ix_workflow_events_run",
            } <= indexes
            _insert_run(connection, "dup", "pending")
        with pytest.raises(Exception):  # the unique idempotency key survived the rebuild
            with engine.begin() as connection:
                _insert_run(connection, "dup", "pending")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Demo helper
# ---------------------------------------------------------------------------


def test_demo_helper_puts_a_failed_run_in_the_queue_and_removes_it_again(db_session, admin):
    from app.db.demo_dead_letter import remove_demo_dead_letters, seed_demo_dead_letter

    run = seed_demo_dead_letter(db_session)

    assert run.status == "failed" and run.attempt_count == 5
    entry = db_session.query(WorkflowDeadLetter).one()
    assert entry.status == "open" and entry.notified_count == 1
    assert [e.event_type for e in _events(db_session, run.id)].count("retry_scheduled") == 4
    assert db_session.query(UserNotification).filter_by(recipient_employee_id=admin).count() == 1

    assert remove_demo_dead_letters(db_session) == 1
    assert db_session.query(WorkflowRun).count() == 0
    assert db_session.query(WorkflowDeadLetter).count() == 0
    assert db_session.query(WorkflowEvent).count() == 0


def test_history_keeps_its_order_even_when_every_timestamp_ties(client, db_session, admin, monkeypatch):
    frozen = datetime.now(timezone.utc)  # every event gets this exact instant
    monkeypatch.setattr(workflow_service, "_now", lambda: frozen)
    run = _run(db_session)
    _fail_always(monkeypatch)

    _drain(db_session)

    kinds = [e.event_type for e in _events(db_session, run.id)]
    assert kinds == ["retry_scheduled"] * 4 + ["dead_lettered"]
    assert [e.sequence for e in _events(db_session, run.id)] == [1, 2, 3, 4, 5]
    shown = client.get(f"/api/v1/workflows/dead-letter/{run.id}").json()["events"]
    assert [e["event_type"] for e in shown] == kinds
