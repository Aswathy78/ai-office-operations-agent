"""Story 4.4 (FR-22): e-mail lifecycle Draft, Approval, Queued, Sending, Sent, Failed, Retry."""
from datetime import datetime, timedelta, timezone
import itertools
import smtplib
import threading
import time
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import email_delivery_service as delivery
from app import email_dispatcher, email_draft_service, email_providers, task_service
from app.db.database import get_db
from app.meeting.models import (
    EMAIL_DRAFT_STATUSES,
    Base,
    EmailDraft,
    EmailDraftEvent,
    Employee,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal

EMPLOYEE = {"actor_id": "user-001", "actor_role": "employee", "actor_team": "Engineering"}
MANAGER = {"actor_id": "user-002", "actor_role": "manager", "actor_team": "Engineering"}
SENDER = {"sender_name": "Rahul", "sender_email": "rahul@northstar.example"}
LATER = timedelta(hours=1)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


@pytest.fixture()
def db_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(db_session: Session):
    def override_get_db():
        yield db_session

    def test_principal(request: Request) -> AuthenticatedPrincipal:
        query = request.query_params
        return AuthenticatedPrincipal(
            user_id=query.get("actor_id", "test-employee"),
            account_id="test-auth-account",
            email="test@example.test",
            name="Test Actor",
            role=query.get("actor_role", "employee"),
            team=query.get("actor_team"),
            jti="test-session",
        )

    original_overrides = app.dependency_overrides.copy()
    startup_handlers = app.router.on_startup[:]
    shutdown_handlers = app.router.on_shutdown[:]
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = test_principal
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        app.router.on_startup[:] = startup_handlers
        app.router.on_shutdown[:] = shutdown_handlers


@pytest.fixture(autouse=True)
def _settings(monkeypatch, db_session):
    monkeypatch.setattr(email_draft_service, "generate_with_llm",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no LLM in tests")))
    monkeypatch.setenv("ORGANIZATION_EMAIL_DOMAINS", "northstar.example")
    for name, email in (("Rahul", "rahul@northstar.example"), ("Ava Patel", None), ("Marcus Chen", None)):
        db_session.add(Employee(
            id=str(uuid.uuid4()), name=name, team="Engineering", is_active=True,
            email=email or f"{name.lower().replace(' ', '.')}@northstar.example",
        ))
    db_session.commit()


REQUESTER = "rahul@northstar.example"


class FakeProvider:
    """Stands in for email_providers.send_email; scripted failures per recipient."""

    def __init__(self, monkeypatch, failures: dict[str, list[Exception]] | None = None):
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls: list[str] = []
        self.observed_status: list[str] = []
        self.session_factory = None
        monkeypatch.setattr(email_providers, "send_email", self)

    @property
    def deliveries(self) -> list[str]:
        """Calls for the e-mail itself (failure notifications to the requester excluded)."""
        return [c for c in self.calls if c != REQUESTER]

    def __call__(self, recipient, subject, body):
        self.calls.append(recipient)
        if self.session_factory is not None:
            # What anyone looking at the DB sees while the provider is working.
            with self.session_factory() as other:
                self.observed_status.append(other.query(EmailDraft.status).scalar())
        queue = self.failures.get(recipient)
        if queue:
            raise queue.pop(0)
        return email_providers.DeliveryReceipt(f"<{len(self.calls)}@provider.example>", "gmail")


def _create(client, prompt="Email Ava and Marcus about the review meeting", actor=EMPLOYEE):
    response = client.post("/api/v1/email-drafts", params=actor, json={"prompt": prompt, **SENDER})
    assert response.status_code == 201, response.text
    return response.json()


def _send(client, draft_id, **extra):
    return client.post(f"/api/v1/email-drafts/{draft_id}/send", params=EMPLOYEE,
                       json={"confirm": True, **extra})


def _get(client, draft_id, actor=EMPLOYEE):
    return client.get(f"/api/v1/email-drafts/{draft_id}", params=actor).json()


def _history(client, draft_id):
    events = client.get(f"/api/v1/email-drafts/{draft_id}/events", params=EMPLOYEE).json()
    return [(e["from_status"], e["to_status"]) for e in events]


def _dispatch(db, now=None):
    db.expire_all()
    return delivery.process_due_emails(db, now=now)


# --- Statuses and transitions -------------------------------------------------------


def test_exactly_seven_lifecycle_statuses():
    assert EMAIL_DRAFT_STATUSES == ("draft", "approval", "queued", "sending", "sent", "failed", "retry")
    assert set(delivery.ALLOWED_TRANSITIONS) == set(EMAIL_DRAFT_STATUSES)


@pytest.mark.parametrize(("source", "target"), list(itertools.product(EMAIL_DRAFT_STATUSES, repeat=2)))
def test_only_lifecycle_transitions_are_allowed(db_session, source, target):
    draft = EmailDraft(
        id=str(uuid.uuid4()), created_by_id="u", created_by_name="U", created_by_role="employee",
        prompt="p", source="template", subject="s", body="b", recipients=[], recipient_groups=[],
        unresolved_recipients=[], approval_required=False, approval_reasons=[],
        sensitive_findings=[], status=source, delivery_results=[],
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db_session.add(draft)
    db_session.commit()
    allowed = {
        ("draft", "approval"), ("draft", "queued"), ("approval", "draft"),
        ("queued", "sending"), ("queued", "draft"), ("sending", "sent"),
        ("sending", "failed"), ("sending", "retry"), ("retry", "sending"), ("failed", "retry"),
        ("sent", "failed"),  # hard bounce reported after acceptance (Story 4.6)
    }
    if (source, target) in allowed:
        delivery.transition(db_session, draft, target, actor_id="tester")
        db_session.commit()
        assert draft.status == target
        event = db_session.query(EmailDraftEvent).one()
        assert (event.from_status, event.to_status, event.actor_id) == (source, target, "tester")
    else:
        with pytest.raises(delivery.InvalidEmailTransition):
            delivery.transition(db_session, draft, target, actor_id="tester")
        assert draft.status == source
        assert db_session.query(EmailDraftEvent).count() == 0


def test_database_rejects_statuses_outside_the_lifecycle(db_session, client):
    draft = _create(client)
    with pytest.raises(Exception):
        db_session.query(EmailDraft).filter_by(id=draft["id"]).update({"status": "partially_sent"})
        db_session.commit()
    db_session.rollback()


# --- Happy path: Draft -> Queued -> Sending -> Sent -------------------------------------


def test_sent_only_after_provider_accepts_every_recipient(client, db_session, session_factory, monkeypatch):
    provider = FakeProvider(monkeypatch)
    provider.session_factory = session_factory
    draft = _create(client)

    queued = _send(client, draft["id"]).json()
    assert queued["status"] == "queued"
    assert queued["sent_at"] is None
    assert provider.calls == []  # queueing never talks to the provider

    summary = _dispatch(db_session)

    assert summary.sent == 1
    # While the provider was working the e-mail was "sending", never "sent".
    assert provider.observed_status == ["sending", "sending"]
    sent = _get(client, draft["id"])
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None
    assert sent["attempt_count"] == 1
    assert {r["email"]: (r["status"], bool(r["message_id"]), bool(r["accepted_at"]))
            for r in sent["delivery_results"]} == {
        "ava.patel@northstar.example": ("sent", True, True),
        "marcus.chen@northstar.example": ("sent", True, True),
    }
    assert _history(client, draft["id"]) == [
        (None, "draft"), ("draft", "queued"), ("queued", "sending"), ("sending", "sent"),
    ]


def test_cancel_while_queued_returns_to_draft(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])

    cancelled = client.post(f"/api/v1/email-drafts/{draft['id']}/cancel", params=EMPLOYEE)

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "draft"
    _dispatch(db_session)
    assert provider.calls == []


def test_cannot_cancel_once_sending_started(client, db_session, monkeypatch):
    FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])
    _dispatch(db_session)

    too_late = client.post(f"/api/v1/email-drafts/{draft['id']}/cancel", params=EMPLOYEE)
    assert too_late.status_code == 409
    assert "already sent" in too_late.json()["detail"]


def test_sent_email_cannot_be_sent_or_edited_again(client, db_session, monkeypatch):
    FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])
    _dispatch(db_session)

    assert _send(client, draft["id"]).status_code == 409
    edit = client.patch(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE, json={"body": "x"})
    assert edit.status_code == 409


# --- Approval -----------------------------------------------------------------------


def test_approval_lifecycle(client, db_session, monkeypatch):
    FakeProvider(monkeypatch)
    draft = _create(client, "Email partner@vendor.com about the contract deadline")
    assert draft["approval_required"] is True

    assert client.post(f"/api/v1/email-drafts/{draft['id']}/submit", params=EMPLOYEE).json()["status"] == "approval"
    pending = client.get("/api/v1/email-drafts/approvals", params=MANAGER).json()
    assert [d["id"] for d in pending] == [draft["id"]]

    approved = client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=MANAGER,
                           json={"decision": "approve"}).json()
    assert (approved["status"], approved["approval_decision"]) == ("draft", "approved")

    assert _send(client, draft["id"]).json()["status"] == "queued"
    _dispatch(db_session)
    assert _get(client, draft["id"])["status"] == "sent"
    assert _history(client, draft["id"]) == [
        (None, "draft"), ("draft", "approval"), ("approval", "draft"),
        ("draft", "queued"), ("queued", "sending"), ("sending", "sent"),
    ]


def test_editing_after_approval_requires_approval_again(client):
    draft = _create(client, "Email partner@vendor.com about the contract deadline")
    client.post(f"/api/v1/email-drafts/{draft['id']}/submit", params=EMPLOYEE)
    client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=MANAGER, json={"decision": "approve"})

    edited = client.patch(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE, json={"body": "New text"}).json()

    assert (edited["status"], edited["approval_decision"]) == ("draft", None)
    assert _send(client, draft["id"]).status_code == 409


def test_rejected_email_returns_to_draft_with_reason(client):
    draft = _create(client, "Email partner@vendor.com about the contract deadline")
    client.post(f"/api/v1/email-drafts/{draft['id']}/submit", params=EMPLOYEE)

    rejected = client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=MANAGER,
                           json={"decision": "reject", "note": "Remove the vendor"}).json()

    assert (rejected["status"], rejected["approval_decision"], rejected["decision_note"]) == (
        "draft", "rejected", "Remove the vendor")
    assert _send(client, draft["id"]).status_code == 409


# --- Failures and retries ---------------------------------------------------------------


def test_temporary_failure_goes_to_retry_with_backoff_then_sends(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch, {
        "marcus.chen@northstar.example": [smtplib.SMTPServerDisconnected("connection lost")],
    })
    draft = _create(client)
    _send(client, draft["id"])
    start = datetime.now(timezone.utc)

    _dispatch(db_session, now=start)
    retry = _get(client, draft["id"])
    assert retry["status"] == "retry"
    assert retry["attempt_count"] == 1
    assert "1 of 2 recipient(s) not delivered" in retry["last_error"]
    assert retry["next_attempt_at"] is not None

    _dispatch(db_session, now=start + timedelta(seconds=5))  # backoff not over yet
    assert _get(client, draft["id"])["status"] == "retry"

    _dispatch(db_session, now=start + LATER)
    sent = _get(client, draft["id"])
    assert sent["status"] == "sent"
    assert sent["attempt_count"] == 2
    assert provider.calls.count("ava.patel@northstar.example") == 1  # never re-sent
    assert ("sending", "retry") in _history(client, draft["id"])


def test_retries_stop_after_max_attempts(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch, {
        "ava.patel@northstar.example": [OSError("timed out")] * 9,
    })
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    now = datetime.now(timezone.utc)
    for _ in range(8):
        _dispatch(db_session, now=now)
        now += LATER

    failed = _get(client, draft["id"])
    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 5  # FR-23: up to 5 attempts
    assert len(provider.deliveries) == 5
    assert [t for _, t in _history(client, draft["id"])][-10:] == [
        "sending", "retry", "sending", "retry", "sending", "retry",
        "sending", "retry", "sending", "failed",
    ]


@pytest.mark.parametrize("error", [
    smtplib.SMTPRecipientsRefused({"ava.patel@northstar.example": (550, b"No such user")}),
    smtplib.SMTPDataError(554, b"Message rejected"),
    ValueError("SMTP_HOST and SMTP_FROM_EMAIL must be configured."),
])
def test_permanent_failure_fails_without_retry(client, db_session, monkeypatch, error):
    FakeProvider(monkeypatch, {"ava.patel@northstar.example": [error]})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])

    _dispatch(db_session)

    failed = _get(client, draft["id"])
    assert failed["status"] == "failed"
    assert failed["sent_at"] is None
    assert failed["next_attempt_at"] is None
    assert failed["delivery_results"][0]["status"] == "failed"


def test_manual_retry_resends_only_undelivered_recipients(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch, {
        "marcus.chen@northstar.example": [smtplib.SMTPRecipientsRefused(
            {"marcus.chen@northstar.example": (550, b"Mailbox full")})],
    })
    draft = _create(client)
    _send(client, draft["id"])
    _dispatch(db_session)
    assert _get(client, draft["id"])["status"] == "failed"

    retry = client.post(f"/api/v1/email-drafts/{draft['id']}/retry", params=EMPLOYEE)
    assert retry.status_code == 200
    assert retry.json()["status"] == "retry"
    _dispatch(db_session)

    assert _get(client, draft["id"])["status"] == "sent"
    assert provider.deliveries == [
        "ava.patel@northstar.example", "marcus.chen@northstar.example", "marcus.chen@northstar.example",
    ]
    assert [t for _, t in _history(client, draft["id"])][-4:] == ["failed", "retry", "sending", "sent"]


def test_only_failed_emails_can_be_retried_and_only_by_sender(client, db_session, monkeypatch):
    FakeProvider(monkeypatch)
    draft = _create(client)
    assert client.post(f"/api/v1/email-drafts/{draft['id']}/retry", params=EMPLOYEE).status_code == 409
    other = {**EMPLOYEE, "actor_id": "someone-else"}
    assert client.post(f"/api/v1/email-drafts/{draft['id']}/retry", params=other).status_code == 403


def test_unexpected_provider_error_never_leaves_email_sending(client, db_session, monkeypatch):
    FakeProvider(monkeypatch, {"ava.patel@northstar.example": [RuntimeError("bug")]})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])

    _dispatch(db_session)

    assert _get(client, draft["id"])["status"] == "failed"


def test_interrupted_sending_is_recovered_as_failed(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    row = db_session.get(EmailDraft, draft["id"])
    row.status = "sending"  # as if the process died mid-delivery
    row.status_changed_at = stale
    db_session.commit()

    summary = _dispatch(db_session)

    assert summary.recovered == 1
    recovered = _get(client, draft["id"])
    assert recovered["status"] == "failed"
    assert "interrupted" in recovered["last_error"]
    assert provider.deliveries == []  # never re-sent blindly


def test_only_one_dispatcher_can_claim_an_email(client, db_session, session_factory, monkeypatch):
    FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])
    now = datetime.now(timezone.utc)

    with session_factory() as first, session_factory() as second:
        assert delivery._claim(first, draft["id"], "queued", now) is True
        assert delivery._claim(second, draft["id"], "queued", now) is False


# --- Provider acceptance (real SMTP call path, fake server) -----------------------------


class FakeSMTP:
    refused: dict = {}
    sent: list = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)
        return dict(FakeSMTP.refused)


@pytest.fixture()
def smtp_config(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@northstar.example")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(task_service.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    FakeSMTP.refused = {}


def test_deliver_email_returns_message_id_when_accepted(smtp_config):
    message_id = task_service.deliver_email("ava@northstar.example", "Hi", "Body")
    assert message_id.startswith("<") and message_id.endswith("@northstar.example>")
    assert FakeSMTP.sent[0]["Message-ID"] == message_id


def test_deliver_email_raises_when_provider_refuses(smtp_config):
    FakeSMTP.refused = {"ava@northstar.example": (550, b"No such user")}
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        task_service.deliver_email("ava@northstar.example", "Hi", "Body")


# --- Dashboards ---------------------------------------------------------------------


def test_failed_and_retry_emails_appear_on_manager_dashboard(client, db_session, monkeypatch):
    FakeProvider(monkeypatch, {
        "ava.patel@northstar.example": [ValueError("SMTP not configured")],
        "marcus.chen@northstar.example": [OSError("timed out")],
    })
    failed = _create(client, "Email Ava about the review meeting")
    retrying = _create(client, "Email Marcus about the review meeting")
    sent = _create(client, "Email Rahul about lunch")  # sender excluded -> no recipients
    for draft in (failed, retrying):
        _send(client, draft["id"])
    _dispatch(db_session)

    issues = client.get("/api/v1/email-drafts/delivery-issues", params=MANAGER)

    assert issues.status_code == 200
    assert {d["id"]: d["status"] for d in issues.json()} == {
        failed["id"]: "failed",
        retrying["id"]: "retry",
    }
    assert sent["id"] not in {d["id"] for d in issues.json()}
    assert client.get("/api/v1/email-drafts/delivery-issues", params=EMPLOYEE).status_code == 403
    mine = client.get("/api/v1/email-drafts", params=EMPLOYEE).json()
    assert {d["id"]: d["status"] for d in mine}[failed["id"]] == "failed"


# --- Background dispatcher ---------------------------------------------------------------


def test_dispatcher_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_DISPATCHER_ENABLED", "false")
    assert email_dispatcher.start_email_dispatcher() is False


def test_wake_runs_a_pass_immediately(monkeypatch):
    """Queueing wakes the dispatcher, so Sending/Sent show up well within 5 seconds."""
    passes: list[float] = []
    stop = threading.Event()
    monkeypatch.setattr(email_dispatcher, "run_once", lambda: passes.append(time.monotonic()) or {})
    thread = threading.Thread(target=email_dispatcher._loop, args=(stop, 30), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not passes and time.monotonic() < deadline:
            time.sleep(0.01)
        woke_at = time.monotonic()
        email_dispatcher.wake()
        while len(passes) < 2 and time.monotonic() < deadline + 2:
            time.sleep(0.01)
        assert len(passes) >= 2
        assert passes[1] - woke_at < 1.0  # not the 30s interval
    finally:
        stop.set()
        email_dispatcher.wake()
        thread.join(timeout=2)


def test_email_times_are_sent_as_utc(client, db_session, monkeypatch):
    """SQLite drops the timezone; the API must still mark times as UTC so browsers show local time."""
    FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])
    _dispatch(db_session)
    sent = _get(client, draft["id"])
    events = client.get(f"/api/v1/email-drafts/{draft['id']}/events", params=EMPLOYEE).json()
    for value in (sent["created_at"], sent["sent_at"], sent["status_changed_at"], events[-1]["created_at"]):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert parsed.utcoffset() == timedelta(0), value
        assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 120, value
