"""Story 4.3 (FR-20): draft an e-mail from chat, preview it, and stay in control of sending."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import email_draft_service
from app.db.database import get_db
from app.email_delivery_service import process_due_emails
from app.email_providers import DeliveryReceipt
from app.email_draft_service import scan_sensitive
from app.meeting.models import Base, Employee
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal

EMPLOYEE = {"actor_id": "user-001", "actor_role": "employee", "actor_team": "Engineering"}
OTHER_EMPLOYEE = {"actor_id": "user-009", "actor_role": "employee", "actor_team": "Finance"}
MANAGER = {"actor_id": "user-002", "actor_role": "manager", "actor_team": "Engineering"}
SENDER = {"sender_name": "Rahul", "sender_email": "rahul@northstar.example"}


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
def no_llm(monkeypatch):
    """Default: the LLM is unavailable, so drafting falls back to templates."""
    def unavailable(*args, **kwargs):
        raise RuntimeError("LLM disabled in tests")

    monkeypatch.setattr(email_draft_service, "generate_with_llm", unavailable)
    monkeypatch.delenv("EMAIL_APPROVAL_RECIPIENT_THRESHOLD", raising=False)
    monkeypatch.setenv("ORGANIZATION_EMAIL_DOMAINS", "northstar.example")


@pytest.fixture()
def sent_emails(monkeypatch):
    """Fake mail provider: accepts every message and returns a Message-ID."""
    sent: list[tuple[str, str, str]] = []

    def accept(recipient, subject, body):
        sent.append((recipient, subject, body))
        return DeliveryReceipt(f"<{len(sent)}@test.example>", "gmail")

    monkeypatch.setattr("app.email_providers.send_email", accept)
    return sent


def _dispatch(db: Session, now=None):
    """Run one pass of the background dispatcher (disabled in tests)."""
    return process_due_emails(db, now=now)


def _employee(db: Session, name: str, team: str | None, email: str | None = None) -> Employee:
    employee = Employee(
        id=str(uuid.uuid4()),
        name=name,
        email=email or f"{name.lower().replace(' ', '.')}@northstar.example",
        team=team,
        is_active=True,
    )
    db.add(employee)
    db.commit()
    return employee


@pytest.fixture()
def directory(db_session: Session):
    _employee(db_session, "Rahul", "Engineering", "rahul@northstar.example")
    _employee(db_session, "Ava Patel", "Engineering")
    _employee(db_session, "Marcus Chen", "Engineering")
    _employee(db_session, "Daniel Brooks", "Engineering")
    # A directory member with a personal domain is still internal.
    _employee(db_session, "Priya Sharma", "Finance", "priya.personal@gmail.com")
    _employee(db_session, "Noah Martin", "Finance")


def _create(client: TestClient, prompt: str, actor=EMPLOYEE, **extra):
    response = client.post(
        "/api/v1/email-drafts", params=actor, json={"prompt": prompt, **SENDER, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_group_resolves_to_recipients_and_template_preview_is_returned(client, directory, sent_emails):
    draft = _create(client, "Email the development team a reminder about tomorrow's sprint demo at 10am")

    assert draft["status"] == "draft"
    assert draft["source"] == "template"
    assert draft["template_key"] == "meeting_reminder"
    assert draft["subject"] == "Reminder: Tomorrow's sprint demo at 10am"
    assert "Hi Engineering team," in draft["body"] and "Rahul" in draft["body"]
    # Sender is not e-mailed their own team message.
    assert {r["email"] for r in draft["recipients"]} == {
        "ava.patel@northstar.example",
        "marcus.chen@northstar.example",
        "daniel.brooks@northstar.example",
    }
    assert draft["recipient_count"] == 3
    assert draft["recipient_groups"] == [{"name": "Engineering team", "count": 3}]
    assert draft["approval_required"] is False
    assert sent_emails == []  # drafting never sends


def test_llm_draft_is_used_when_no_template_matches(client, directory, monkeypatch):
    calls = []

    def fake_llm(prompt, *, sender, greeting):
        calls.append((prompt, sender, greeting))
        return "Q3 planning kickoff", "Hi Finance,\n\nLet's kick off Q3 planning.\n\nRahul"

    monkeypatch.setattr(email_draft_service, "generate_with_llm", fake_llm)
    draft = _create(client, "Write to the finance team to kick off Q3 planning")

    assert draft["source"] == "llm"
    assert draft["subject"] == "Q3 planning kickoff"
    assert calls == [("Write to the finance team to kick off Q3 planning", "Rahul", "Finance team")]


def test_llm_failure_falls_back_to_general_template_with_note(client, directory):
    draft = _create(client, "Send the finance team the Q3 budget summary")

    assert draft["source"] == "template"
    assert draft["template_key"] == "general"
    assert "AI drafting is unavailable" in draft["generation_note"]
    assert draft["subject"] == "The Q3 budget summary"


def test_external_recipient_requires_approval_before_send(client, db_session, directory, sent_emails):
    draft = _create(client, "Email partner@vendor.com and Ava about the contract renewal deadline")

    assert {r["email"]: r["internal"] for r in draft["recipients"]} == {
        "ava.patel@northstar.example": True,
        "partner@vendor.com": False,
    }
    assert draft["approval_required"] is True
    assert any("partner@vendor.com" in reason for reason in draft["approval_reasons"])

    blocked = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE,
                          json={"confirm": True})
    assert blocked.status_code == 409
    assert "Approval is required" in blocked.json()["detail"]

    submitted = client.post(f"/api/v1/email-drafts/{draft['id']}/submit", params=EMPLOYEE)
    assert submitted.json()["status"] == "approval"

    queue = client.get("/api/v1/email-drafts/approvals", params=MANAGER).json()
    assert [item["id"] for item in queue] == [draft["id"]]

    decided = client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=MANAGER,
                          json={"decision": "approve", "note": "OK"})
    # Approval returns the e-mail to Draft with the decision recorded; it does not send.
    assert decided.json()["status"] == "draft"
    assert decided.json()["approval_decision"] == "approved"
    assert sent_emails == []

    queued = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE,
                         json={"confirm": True})
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued"
    _dispatch(db_session)
    sent = client.get(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE).json()
    assert sent["status"] == "sent"
    assert sorted(r for r, _, _ in sent_emails) == ["ava.patel@northstar.example", "partner@vendor.com"]


def test_directory_member_with_personal_domain_is_internal(client, directory):
    draft = _create(client, "Send the finance team a reminder about the review meeting")
    assert draft["recipient_count"] == 2
    assert all(r["internal"] for r in draft["recipients"])
    assert draft["approval_required"] is False


def test_more_than_ten_recipients_requires_approval(client, db_session):
    for index in range(11):
        _employee(db_session, f"Ops Person{index}", "Operations")

    draft = _create(client, "Email the operations team about the office move")

    assert draft["recipient_count"] == 11
    assert draft["approval_required"] is True
    assert draft["approval_reasons"][0].startswith("11 recipients exceeds the limit of 10")


def test_editing_an_approved_draft_invalidates_approval(client, directory):
    draft = _create(client, "Email partner@vendor.com about the deadline for the contract")
    client.post(f"/api/v1/email-drafts/{draft['id']}/submit", params=EMPLOYEE)
    client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=MANAGER,
                json={"decision": "approve"})

    edited = client.patch(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE,
                          json={"body": "Changed after approval"}).json()

    assert edited["status"] == "draft"
    assert edited["decided_by"] is None
    blocked = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE,
                          json={"confirm": True})
    assert blocked.status_code == 409


def test_sensitive_data_must_be_acknowledged_before_send(client, db_session, directory, sent_emails):
    draft = _create(client, "Email Ava about the review meeting")
    updated = client.patch(
        f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE,
        json={"body": "Card 4111 1111 1111 1111, password: hunter2. Confidential."},
    ).json()

    labels = {f["label"] for f in updated["sensitive_findings"]}
    assert {"Payment card number", "Password or PIN", "Confidentiality marker"} <= labels
    card = next(f for f in updated["sensitive_findings"] if f["type"] == "credit_card")
    assert "1111 1111 1111" not in card["match"]  # masked in the preview

    blocked = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE,
                          json={"confirm": True})
    assert blocked.status_code == 409
    assert "sensitive" in blocked.json()["detail"]
    assert sent_emails == []

    queued = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE,
                         json={"confirm": True, "acknowledge_sensitive": True})
    assert queued.json()["status"] == "queued"
    assert queued.json()["acknowledged_sensitive"] is True
    _dispatch(db_session)
    assert client.get(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE).json()["status"] == "sent"
    assert len(sent_emails) == 1


def test_send_requires_explicit_confirmation_and_owner(client, directory, sent_emails):
    draft = _create(client, "Email Ava about the review meeting")

    unconfirmed = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE, json={})
    assert unconfirmed.status_code == 422

    not_owner = client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=OTHER_EMPLOYEE,
                            json={"confirm": True})
    assert not_owner.status_code == 403
    assert sent_emails == []


def test_cannot_approve_own_email_and_employees_cannot_approve(client, directory):
    draft = _create(client, "Email partner@vendor.com about the deadline", actor=MANAGER)
    client.post(f"/api/v1/email-drafts/{draft['id']}/submit", params=MANAGER)

    own = client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=MANAGER,
                      json={"decision": "approve"})
    assert own.status_code == 403
    employee = client.post(f"/api/v1/email-drafts/{draft['id']}/decision", params=EMPLOYEE,
                           json={"decision": "approve"})
    assert employee.status_code == 403


def test_recipients_can_be_removed_and_groups_added(client, directory):
    draft = _create(client, "Email the development team about the release")

    edited = client.patch(
        f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE,
        json={"recipient_emails": ["ava.patel@northstar.example"], "add_groups": ["Finance"]},
    ).json()

    assert {r["email"] for r in edited["recipients"]} == {
        "ava.patel@northstar.example", "priya.personal@gmail.com", "noah.martin@northstar.example",
    }
    assert edited["recipient_groups"] == [
        {"name": "Engineering team", "count": 1},
        {"name": "Finance team", "count": 2},
    ]

    invalid = client.patch(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE,
                           json={"recipient_emails": ["not-an-email"]})
    assert invalid.status_code == 422


def test_failed_delivery_can_be_retried_without_resending(client, db_session, directory, monkeypatch):
    attempts: list[str] = []

    def flaky(recipient, subject, body):
        attempts.append(recipient)
        if recipient.startswith("marcus") and attempts.count(recipient) == 1:
            raise OSError("SMTP down")
        return DeliveryReceipt(f"<{len(attempts)}@test.example>", "gmail")

    monkeypatch.setattr("app.email_providers.send_email", flaky)
    draft = _create(client, "Email Ava and Marcus about the review meeting")

    client.post(f"/api/v1/email-drafts/{draft['id']}/send", params=EMPLOYEE, json={"confirm": True})
    _dispatch(db_session)
    first = client.get(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE).json()
    assert first["status"] == "retry"  # temporary failure: retried automatically

    _dispatch(db_session, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    retry = client.get(f"/api/v1/email-drafts/{draft['id']}", params=EMPLOYEE).json()
    assert retry["status"] == "sent"
    assert attempts.count("ava.patel@northstar.example") == 1


def test_directory_groups_endpoint_reports_counts(client, directory):
    groups = client.get("/api/v1/email-drafts/directory/groups").json()
    assert groups == [
        {"name": "Engineering", "count": 4},
        {"name": "Finance", "count": 2},
        {"name": "Everyone", "count": 6},
    ]


def test_scan_ignores_ordinary_text():
    assert scan_sensitive("Sprint demo", "See you at 10am in room 4. Account information is in the wiki.") == []
