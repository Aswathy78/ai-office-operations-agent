"""Story 4.6 (FR-23): retry with backoff, escalation, bounces, provider fallback."""
from datetime import datetime, timedelta, timezone
import json
import smtplib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import email_delivery_service as delivery
from app import email_draft_service, email_escalation_service, email_providers
from app.db.database import get_db
from app.meeting.models import Base, EmailDraft, EmailFailureNotification, Employee

EMPLOYEE = {"actor_id": "user-001", "actor_role": "employee", "actor_team": "Engineering"}
MANAGER = {"actor_id": "user-002", "actor_role": "manager", "actor_team": "Engineering"}
ADMIN = {"actor_id": "user-004", "actor_role": "admin", "actor_team": "Administration"}
REQUESTER = "rahul@northstar.example"
SENDER = {"sender_name": "Rahul", "sender_email": REQUESTER}
TOKEN = "test-bounce-token"


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
def client(db_session: Session, monkeypatch):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        # Startup reloads backend/.env; set this test's configuration afterwards.
        monkeypatch.setenv("SYSTEM_ADMIN_EMAILS", "it-admin@northstar.example, ops-admin@northstar.example")
        monkeypatch.setenv("EMAIL_BOUNCE_WEBHOOK_TOKEN", TOKEN)
        monkeypatch.setenv("ORGANIZATION_EMAIL_DOMAINS", "northstar.example")
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _directory(monkeypatch, db_session):
    monkeypatch.setattr(email_draft_service, "generate_with_llm",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no LLM in tests")))
    for name, email in (("Rahul", REQUESTER), ("Ava Patel", None), ("Marcus Chen", None)):
        db_session.add(Employee(
            id=str(uuid.uuid4()), name=name, team="Engineering", is_active=True,
            email=email or f"{name.lower().replace(' ', '.')}@northstar.example",
        ))
    db_session.commit()


class FakeProvider:
    """Stands in for email_providers.send_email; scripted errors per address."""

    def __init__(self, monkeypatch, failures: dict[str, list[Exception]] | None = None):
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls: list[tuple[str, str]] = []
        monkeypatch.setattr(email_providers, "send_email", self)

    def __call__(self, recipient, subject, body):
        self.calls.append((recipient, subject))
        queue = self.failures.get(recipient)
        if queue:
            raise queue.pop(0)
        return email_providers.DeliveryReceipt(f"<{uuid.uuid4().hex}@provider.example>", "gmail")

    def to(self, address: str) -> list[str]:
        return [subject for recipient, subject in self.calls if recipient == address]


def _create(client, prompt="Email Ava and Marcus about the review meeting"):
    response = client.post("/api/v1/email-drafts", params=EMPLOYEE, json={"prompt": prompt, **SENDER})
    assert response.status_code == 201, response.text
    return response.json()


def _send(client, draft_id):
    response = client.post(f"/api/v1/email-drafts/{draft_id}/send", params=EMPLOYEE, json={"confirm": True})
    assert response.status_code == 200, response.text


def _get(client, draft_id):
    return client.get(f"/api/v1/email-drafts/{draft_id}", params=EMPLOYEE).json()


def _dispatch(db, now=None):
    db.expire_all()
    return delivery.process_due_emails(db, now=now)


# --- Retry schedule ------------------------------------------------------------------


def test_backoff_is_1_5_15_minutes_and_up_to_5_attempts():
    assert delivery.max_attempts() == 5
    assert [delivery.retry_backoff_seconds(n) for n in (1, 2, 3, 4)] == [60, 300, 900, 900]


def test_transient_errors_retry_on_schedule_then_fail(client, db_session, monkeypatch):
    FakeProvider(monkeypatch, {"ava.patel@northstar.example": [smtplib.SMTPServerDisconnected("gone")] * 9})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    start = datetime.now(timezone.utc)

    _dispatch(db_session, now=start)
    first = _get(client, draft["id"])
    gap = datetime.fromisoformat(first["next_attempt_at"]).replace(tzinfo=timezone.utc) - start
    assert first["status"] == "retry" and 55 <= gap.total_seconds() <= 70  # 1 minute

    _dispatch(db_session, now=start + timedelta(seconds=30))  # not due yet
    assert _get(client, draft["id"])["attempt_count"] == 1

    now = start
    expected_gaps = [300, 900, 900]  # after attempts 2, 3 and 4
    for gap_seconds in expected_gaps:
        now += timedelta(hours=1)
        _dispatch(db_session, now=now)
        current = _get(client, draft["id"])
        assert current["status"] == "retry"
        scheduled = datetime.fromisoformat(current["next_attempt_at"]).replace(tzinfo=timezone.utc)
        assert abs((scheduled - now).total_seconds() - gap_seconds) < 15

    _dispatch(db_session, now=now + timedelta(hours=1))
    final = _get(client, draft["id"])
    assert final["status"] == "failed"
    assert final["attempt_count"] == 5


# --- Escalation ----------------------------------------------------------------------


def test_final_failure_notifies_requester_and_admins(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch, {
        "ava.patel@northstar.example": [ValueError("SMTP_HOST and SMTP_FROM_EMAIL must be configured.")],
    })
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])

    _dispatch(db_session)

    failed = _get(client, draft["id"])
    assert failed["status"] == "failed"
    assert failed["escalated_at"] is not None
    by_audience = {(n["audience"], n["channel"], n["recipient_email"]): n["status"] for n in failed["notifications"]}
    assert by_audience == {
        ("requester", "email", REQUESTER): "sent",
        ("admin", "email", "it-admin@northstar.example"): "sent",
        ("admin", "email", "ops-admin@northstar.example"): "sent",
        ("admin", "in_app", None): "recorded",
    }
    assert provider.to(REQUESTER) == [f"E-mail delivery failed: {failed['subject']}"]
    assert len(provider.to("it-admin@northstar.example")) == 1


def test_escalation_is_not_repeated_for_the_same_failure(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch, {"ava.patel@northstar.example": [ValueError("bad config")]})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    _dispatch(db_session)
    _dispatch(db_session)
    email_escalation_service.notify_failure(db_session, db_session.get(EmailDraft, draft["id"]))

    assert len(provider.to(REQUESTER)) == 1
    assert db_session.query(EmailFailureNotification).count() == 4


def test_failed_notification_is_retried_later(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch, {
        "ava.patel@northstar.example": [ValueError("bad config")],
        REQUESTER: [OSError("mail is down")],
    })
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    _dispatch(db_session)
    note = next(n for n in _get(client, draft["id"])["notifications"] if n["recipient_email"] == REQUESTER)
    assert note["status"] == "failed" and note["next_attempt_at"] is not None

    _dispatch(db_session, now=datetime.now(timezone.utc) + timedelta(minutes=2))

    note = next(n for n in _get(client, draft["id"])["notifications"] if n["recipient_email"] == REQUESTER)
    assert note["status"] == "sent"
    assert note["attempts"] == 2
    assert len(provider.to(REQUESTER)) == 2


def test_admin_is_always_notified_in_app_even_without_admin_emails(client, db_session, monkeypatch):
    monkeypatch.delenv("SYSTEM_ADMIN_EMAILS")
    FakeProvider(monkeypatch, {"ava.patel@northstar.example": [ValueError("bad config")]})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    _dispatch(db_session)

    channels = {(n["audience"], n["channel"]) for n in _get(client, draft["id"])["notifications"]}
    assert channels == {("requester", "email"), ("admin", "in_app")}


# --- Bounces --------------------------------------------------------------------------


def test_5xx_rejection_is_recorded_as_a_bounce(client, db_session, monkeypatch):
    FakeProvider(monkeypatch, {"ava.patel@northstar.example": [
        smtplib.SMTPRecipientsRefused({"ava.patel@northstar.example": (550, b"5.1.1 No such user")}),
    ]})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    _dispatch(db_session)

    failed = _get(client, draft["id"])
    assert failed["status"] == "failed"
    assert [(b["recipient"], b["type"], b["source"], b["code"]) for b in failed["bounces"]] == [
        ("ava.patel@northstar.example", "hard", "smtp", 550),
    ]
    assert "No such user" in failed["bounces"][0]["diagnostic"]


def _sent_email(client, db_session, monkeypatch):
    provider = FakeProvider(monkeypatch)
    draft = _create(client)
    _send(client, draft["id"])
    _dispatch(db_session)
    sent = _get(client, draft["id"])
    assert sent["status"] == "sent"
    return provider, sent


def test_hard_bounce_after_sent_marks_failed_and_escalates(client, db_session, monkeypatch):
    provider, sent = _sent_email(client, db_session, monkeypatch)
    marcus = next(r for r in sent["delivery_results"] if r["email"].startswith("marcus"))

    response = client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": TOKEN},
                           json={"message_id": marcus["message_id"], "bounce_type": "hard",
                                 "diagnostic": "550 5.1.1 Mailbox does not exist"})

    assert response.json() == {"recorded": 1, "unmatched": [], "draft_ids": [sent["id"]]}
    failed = _get(client, sent["id"])
    assert failed["status"] == "failed"  # never left showing Sent
    assert {r["email"]: r["status"] for r in failed["delivery_results"]} == {
        "ava.patel@northstar.example": "sent",
        "marcus.chen@northstar.example": "bounced",
    }
    assert failed["bounces"][0]["type"] == "hard"
    assert failed["bounces"][0]["source"] == "dsn"
    assert len(provider.to(REQUESTER)) == 1  # requester told
    events = client.get(f"/api/v1/email-drafts/{sent['id']}/events", params=EMPLOYEE).json()
    assert (events[-1]["from_status"], events[-1]["to_status"]) == ("sent", "failed")


def test_soft_bounce_is_recorded_without_changing_status(client, db_session, monkeypatch):
    _, sent = _sent_email(client, db_session, monkeypatch)
    message_id = sent["delivery_results"][0]["message_id"]

    client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": TOKEN},
                json={"message_id": message_id, "bounce_type": "soft", "diagnostic": "Mailbox full"})

    after = _get(client, sent["id"])
    assert after["status"] == "sent"
    assert [(b["type"], b["diagnostic"]) for b in after["bounces"]] == [("soft", "Mailbox full")]


def test_duplicate_bounce_reports_are_recorded_once(client, db_session, monkeypatch):
    provider, sent = _sent_email(client, db_session, monkeypatch)
    payload = {"message_id": sent["delivery_results"][0]["message_id"], "bounce_type": "hard"}
    for _ in range(3):
        client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": TOKEN}, json=payload)

    assert len(_get(client, sent["id"])["bounces"]) == 1
    assert len(provider.to(REQUESTER)) == 1


def test_ses_bounce_notification_via_sns(client, db_session, monkeypatch):
    _, sent = _sent_email(client, db_session, monkeypatch)
    ava = sent["delivery_results"][0]
    ses = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": "Permanent", "bouncedRecipients": [
            {"emailAddress": ava["email"], "status": "5.1.1", "diagnosticCode": "smtp; 550 5.1.1 user unknown"},
        ]},
        "mail": {"messageId": "ses-internal-id", "commonHeaders": {"messageId": ava["message_id"]}},
    }
    sns = {"Type": "Notification", "Message": json.dumps(ses)}

    response = client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": TOKEN}, json=sns)

    assert response.json()["recorded"] == 1
    bounce = _get(client, sent["id"])["bounces"][0]
    assert (bounce["source"], bounce["type"], bounce["code"]) == ("ses", "hard", 550)


def test_bounce_webhook_requires_token_or_admin(client, db_session, monkeypatch):
    _, sent = _sent_email(client, db_session, monkeypatch)
    payload = {"message_id": sent["delivery_results"][0]["message_id"], "bounce_type": "soft"}

    assert client.post("/api/v1/email-drafts/bounces", json=payload).status_code == 403
    assert client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": "wrong"},
                       json=payload).status_code == 403
    assert client.post("/api/v1/email-drafts/bounces", params=MANAGER, json=payload).status_code == 403
    assert client.post("/api/v1/email-drafts/bounces", params=ADMIN, json=payload).status_code == 403


def test_unknown_message_id_is_reported_unmatched(client):
    response = client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": TOKEN},
                           json={"message_id": "<nobody@nowhere>", "bounce_type": "hard"})
    assert response.json() == {"recorded": 0, "unmatched": ["<nobody@nowhere>"], "draft_ids": []}


def test_sns_subscription_confirmation_is_not_auto_confirmed(client):
    response = client.post("/api/v1/email-drafts/bounces", headers={"X-Bounce-Webhook-Token": TOKEN},
                           json={"Type": "SubscriptionConfirmation", "SubscribeURL": "https://sns.example/confirm"})
    assert response.status_code == 202
    assert "https://sns.example/confirm" in response.json()["detail"]


# --- A failed e-mail is never shown as Sent ------------------------------------------------


@pytest.mark.parametrize("failure", [
    smtplib.SMTPRecipientsRefused({"marcus.chen@northstar.example": (550, b"No such user")}),
    smtplib.SMTPDataError(554, b"Rejected"),
    ValueError("not configured"),
    RuntimeError("bug"),
])
def test_partial_or_total_failure_is_never_sent(client, db_session, monkeypatch, failure):
    FakeProvider(monkeypatch, {"marcus.chen@northstar.example": [failure] * 9})
    draft = _create(client)
    _send(client, draft["id"])
    now = datetime.now(timezone.utc)
    seen = set()
    for _ in range(7):
        _dispatch(db_session, now=now)
        seen.add(_get(client, draft["id"])["status"])
        now += timedelta(hours=1)

    assert "sent" not in seen
    assert _get(client, draft["id"])["status"] == "failed"
    issues = client.get("/api/v1/email-drafts/delivery-issues", params=ADMIN).json()
    assert draft["id"] in {i["id"] for i in issues}


# --- Provider fallback (Gmail -> SMTP/SES) ---------------------------------------------------


class RoutingSMTP:
    """smtplib.SMTP stand-in whose behaviour depends on the host it connects to."""

    behaviour: dict[str, Exception | None] = {}
    sent: list[tuple[str, str]] = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.esmtp_features = {"auth": "PLAIN LOGIN"}
        error = RoutingSMTP.behaviour.get(host)
        if isinstance(error, (OSError, smtplib.SMTPConnectError)):
            raise error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def has_extn(self, name):
        return name == "auth"

    def auth_plain(self, _challenge=None):
        return "test"

    def auth_login(self, _challenge=None):
        return "test"

    def auth(self, mechanism, _authobject):
        error = RoutingSMTP.behaviour.get(self.host)
        if isinstance(error, smtplib.SMTPAuthenticationError):
            raise error
        return 235, b"Accepted"

    def send_message(self, message):
        error = RoutingSMTP.behaviour.get(self.host)
        if isinstance(error, smtplib.SMTPException) and not isinstance(error, smtplib.SMTPAuthenticationError):
            raise error
        RoutingSMTP.sent.append((self.host, message["To"]))
        return {}


@pytest.fixture()
def two_providers(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "office@northstar.example")
    monkeypatch.setenv("SMTP_USERNAME", "office")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("FALLBACK_SMTP_HOST", "email-smtp.us-east-1.amazonaws.com")
    monkeypatch.setenv("FALLBACK_SMTP_USERNAME", "ses-user")
    monkeypatch.setenv("FALLBACK_SMTP_PASSWORD", "ses-secret")
    monkeypatch.setattr(smtplib, "SMTP", RoutingSMTP)
    RoutingSMTP.behaviour = {}
    RoutingSMTP.sent = []


@pytest.mark.parametrize("primary_error", [
    ConnectionRefusedError("connection refused"),
    TimeoutError("timed out"),
    smtplib.SMTPAuthenticationError(535, b"Bad credentials"),
    smtplib.SMTPDataError(421, b"Service not available"),
])
def test_falls_back_to_ses_when_gmail_unavailable(two_providers, primary_error):
    RoutingSMTP.behaviour = {"smtp.gmail.com": primary_error}

    receipt = email_providers.send_email("ava@northstar.example", "Hi", "Body")

    assert receipt.provider == "ses"
    assert receipt.fallback_used is True
    assert RoutingSMTP.sent == [("email-smtp.us-east-1.amazonaws.com", "ava@northstar.example")]


def test_primary_used_when_available(two_providers):
    receipt = email_providers.send_email("ava@northstar.example", "Hi", "Body")
    assert (receipt.provider, receipt.fallback_used) == ("gmail", False)
    assert RoutingSMTP.sent == [("smtp.gmail.com", "ava@northstar.example")]


def test_disconnect_during_smtp_context_exit_cannot_retry_an_accepted_message(
    two_providers, monkeypatch,
):
    class DisconnectOnExitSMTP(RoutingSMTP):
        def __exit__(self, *exc):
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    RoutingSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", DisconnectOnExitSMTP)

    receipt = email_providers.send_email("ava@northstar.example", "Hi", "Body")

    assert receipt.provider == "gmail"
    assert receipt.fallback_used is False
    assert RoutingSMTP.sent == [("smtp.gmail.com", "ava@northstar.example")]


def test_recipient_rejection_does_not_use_fallback(two_providers):
    RoutingSMTP.behaviour = {"smtp.gmail.com": smtplib.SMTPRecipientsRefused({"ava@northstar.example": (550, b"No")})}
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        email_providers.send_email("ava@northstar.example", "Hi", "Body")
    assert RoutingSMTP.sent == []


def test_without_fallback_configured_the_primary_error_is_raised(two_providers, monkeypatch):
    monkeypatch.delenv("FALLBACK_SMTP_HOST")
    RoutingSMTP.behaviour = {"smtp.gmail.com": ConnectionRefusedError("down")}
    with pytest.raises(ConnectionRefusedError):
        email_providers.send_email("ava@northstar.example", "Hi", "Body")


def test_both_providers_down_is_a_transient_retry(client, db_session, two_providers):
    RoutingSMTP.behaviour = {
        "smtp.gmail.com": ConnectionRefusedError("gmail down"),
        "email-smtp.us-east-1.amazonaws.com": TimeoutError("ses timed out"),
    }
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    _dispatch(db_session)
    assert _get(client, draft["id"])["status"] == "retry"


def test_fallback_delivery_is_recorded_on_the_email(client, db_session, two_providers):
    RoutingSMTP.behaviour = {"smtp.gmail.com": ConnectionRefusedError("gmail down")}
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])
    _dispatch(db_session)

    sent = _get(client, draft["id"])
    assert sent["status"] == "sent"
    assert [(r["provider"], r["fallback_used"]) for r in sent["delivery_results"]] == [("ses", True)]


# --- A rejected mail login (the sender's problem, not the recipient's) ----------------


def test_a_rejected_mail_login_is_not_retried_not_a_bounce_and_alerts_the_administrator(
    client, db_session, monkeypatch,
):
    from app.meeting.models import AuthAccount, UserNotification
    from app.plain_errors import SIGN_IN_REJECTED

    admin = Employee(id=str(uuid.uuid4()), name="Ethan Walker", team="Operations", is_active=True,
                     email="ethan.walker@northstar.example")
    db_session.add(admin)
    db_session.add(AuthAccount(id="acc-admin", employee_id=admin.id, email_normalized=admin.email,
                               password_hash="x", role="admin", is_enabled=True))
    db_session.commit()
    provider = FakeProvider(monkeypatch, {"ava.patel@northstar.example": [
        smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"),
    ] * 5})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])

    _dispatch(db_session)

    failed = _get(client, draft["id"])
    assert failed["status"] == "failed" and failed["attempt_count"] == 1, "no pointless retries"
    assert len(provider.to("ava.patel@northstar.example")) == 1
    assert failed["bounces"] == [], "the recipient's address was fine; the sender's login was not"
    assert failed["last_error"] == "1 of 1 recipient(s) not delivered. " + SIGN_IN_REJECTED
    alerts = db_session.query(UserNotification).filter_by(entity_type="system", entity_id="mail").all()
    assert [a.recipient_employee_id for a in alerts] == [admin.id]
    assert alerts[0].email_status == "disabled", "an alert about broken e-mail is not sent by e-mail"


def test_a_real_recipient_rejection_is_still_a_bounce(client, db_session, monkeypatch):
    FakeProvider(monkeypatch, {"ava.patel@northstar.example": [
        smtplib.SMTPRecipientsRefused({"ava.patel@northstar.example": (550, b"5.1.1 No such user")}),
    ]})
    draft = _create(client, "Email Ava about the review meeting")
    _send(client, draft["id"])

    _dispatch(db_session)

    assert [b["code"] for b in _get(client, draft["id"])["bounces"]] == [550]
