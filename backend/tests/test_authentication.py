from datetime import datetime, timedelta, timezone
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth_service import provision_account
from app.authentication import require_authenticated_actor_id
from app.db.database import get_db
from app.meeting.models import (
    AuthAccount,
    AuthSession,
    Base,
    Employee,
    Meeting,
    MeetingDelegate,
    MeetingParticipant,
)
from main import app

AUTH_SECRET = "test-only-secret-that-is-at-least-32-bytes-long"
AUTH_ISSUER = "test-ai-office"
AUTH_AUDIENCE = "test-ai-office-client"
PASSWORD = "correct horse battery staple"


@pytest.fixture()
def db_session(tmp_path):
    database_path = (tmp_path / "authentication-test.db").as_posix()
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
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
def client(db_session: Session, monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", AUTH_SECRET)
    monkeypatch.setenv("AUTH_JWT_ISSUER", AUTH_ISSUER)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", AUTH_AUDIENCE)
    monkeypatch.setenv("AUTH_ACCESS_TOKEN_TTL_SECONDS", "900")
    previous_overrides = app.dependency_overrides.copy()
    request_session_factory = sessionmaker(
        bind=db_session.get_bind(),
        autoflush=False,
        autocommit=False,
    )

    def override_get_db():
        request_db = request_session_factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


def make_employee(
    db: Session,
    employee_id: str,
    email: str,
    *,
    active: bool = True,
) -> Employee:
    employee = Employee(
        id=employee_id,
        name=f"Employee {employee_id}",
        email=email,
        team="Engineering",
        is_active=active,
    )
    db.add(employee)
    db.commit()
    return employee


def make_account(
    db: Session,
    employee: Employee,
    *,
    role: str = "employee",
    password: str = PASSWORD,
) -> AuthAccount:
    return provision_account(db, employee.email, password, role)


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_token(
    db: Session,
    account: AuthAccount,
    *,
    expired: bool = False,
    signing_secret: str = AUTH_SECRET,
    extra_claims: dict | None = None,
    algorithm: str = "HS256",
    register_session: bool = True,
) -> str:
    now = datetime.now(timezone.utc)
    token_id = str(uuid.uuid4())
    expires_at = now - timedelta(minutes=1) if expired else now + timedelta(minutes=10)
    if register_session:
        db.add(
            AuthSession(
                jti=token_id,
                account_id=account.id,
                expires_at=expires_at,
            )
        )
        db.commit()
    claims = {
        "sub": account.id,
        "iss": AUTH_ISSUER,
        "aud": AUTH_AUDIENCE,
        "iat": int((now - timedelta(minutes=2)).timestamp()) if expired else int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": token_id,
        **(extra_claims or {}),
    }
    return jwt.encode(claims, signing_secret, algorithm=algorithm)


def test_login_me_logout_uses_employee_mapping_and_database_role(
    client: TestClient,
    db_session: Session,
):
    employee = make_employee(db_session, "employee-1", "Worker@Example.test")
    account = make_account(db_session, employee, role="employee")

    response = login(client, "  WORKER@example.test  ")

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["user"] == {
        "id": employee.id,
        "account_id": account.id,
        "name": employee.name,
        "email": employee.email,
        "role": "employee",
        "team": "Engineering",
    }

    current = client.get("/api/v1/auth/me", headers=bearer(body["access_token"]))
    assert current.status_code == 200
    assert current.json()["id"] == employee.id
    assert current.json()["role"] == account.role

    logout = client.post("/api/v1/auth/logout", headers=bearer(body["access_token"]))
    assert logout.status_code == 204
    assert client.get("/api/v1/auth/me", headers=bearer(body["access_token"])).status_code == 401


def test_invalid_password_and_unknown_account_have_same_safe_response(
    client: TestClient,
    db_session: Session,
):
    employee = make_employee(db_session, "employee-1", "worker@example.test")
    make_account(db_session, employee)

    invalid_password = login(client, "worker@example.test", "incorrect password")
    unknown_account = login(client, "unknown@example.test", "incorrect password")

    assert invalid_password.status_code == unknown_account.status_code == 401
    assert invalid_password.json() == unknown_account.json() == {
        "detail": "Invalid email or password."
    }


def test_missing_invalid_and_expired_tokens_are_rejected(
    client: TestClient,
    db_session: Session,
):
    employee = make_employee(db_session, "employee-1", "worker@example.test")
    account = make_account(db_session, employee)

    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer("not-a-jwt")).status_code == 401
    wrong_signature = make_token(
        db_session,
        account,
        signing_secret="different-test-secret-with-enough-entropy",
    )
    assert client.get("/api/v1/auth/me", headers=bearer(wrong_signature)).status_code == 401
    expired = make_token(db_session, account, expired=True)
    assert client.get("/api/v1/auth/me", headers=bearer(expired)).status_code == 401


def test_jwt_rejects_wrong_issuer_audience_algorithm_and_unregistered_jti(
    client: TestClient,
    db_session: Session,
):
    employee = make_employee(db_session, "employee-1", "worker@example.test")
    account = make_account(db_session, employee)
    tokens = [
        make_token(db_session, account, extra_claims={"iss": "untrusted-issuer"}),
        make_token(db_session, account, extra_claims={"aud": "other-client"}),
        make_token(db_session, account, algorithm="HS384"),
        make_token(db_session, account, register_session=False),
    ]

    assert all(
        client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
        for token in tokens
    )


@pytest.mark.parametrize("disabled", ["account", "employee"])
def test_disabled_account_or_inactive_employee_cannot_login(
    client: TestClient,
    db_session: Session,
    disabled: str,
):
    employee = make_employee(db_session, "employee-1", "worker@example.test")
    account = make_account(db_session, employee)
    if disabled == "account":
        account.is_enabled = False
        db_session.commit()
    else:
        employee.is_active = False
        db_session.commit()

    response = login(client, "worker@example.test")
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


def test_token_cannot_select_identity_or_role_and_unmapped_employee_is_rejected(
    client: TestClient,
    db_session: Session,
):
    employee = make_employee(db_session, "employee-1", "worker@example.test")
    account = make_account(db_session, employee, role="employee")
    forged_claims = make_token(
        db_session,
        account,
        extra_claims={"role": "admin", "user_id": "employee-attacker"},
    )

    current = client.get("/api/v1/auth/me", headers=bearer(forged_claims))
    assert current.status_code == 200
    assert current.json()["id"] == employee.id
    assert current.json()["role"] == "employee"

    unmapped_account = AuthAccount(
        id=str(uuid.uuid4()),
        employee_id="missing-employee",
        email_normalized="orphan@example.test",
        password_hash=account.password_hash,
        role="employee",
        is_enabled=True,
    )
    db_session.add(unmapped_account)
    db_session.commit()
    unmapped_token = make_token(db_session, unmapped_account)

    assert client.get("/api/v1/auth/me", headers=bearer(unmapped_token)).status_code == 401


def test_provisioning_requires_existing_active_employee_and_defaults_to_employee(db_session: Session):
    with pytest.raises(ValueError, match="exactly one existing employee"):
        provision_account(db_session, "new@example.test", PASSWORD)

    employee = make_employee(db_session, "employee-1", "Worker@Example.test")
    account = provision_account(db_session, employee.email, PASSWORD)
    assert account.employee_id == employee.id
    assert account.email_normalized == "worker@example.test"
    assert account.role == "employee"
    assert account.password_hash.startswith("$argon2id$")
    assert account.password_hash != PASSWORD

    with pytest.raises(ValueError, match="already exists"):
        provision_account(db_session, employee.email, PASSWORD)


def test_spoofed_meeting_identity_is_unauthorized_and_role_is_not_granted(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    employee = make_employee(db_session, "employee-1", "worker@example.test")
    other_employee = make_employee(db_session, "employee-2", "other@example.test")
    make_account(db_session, employee, role="employee")
    make_account(db_session, other_employee, role="employee")
    meeting = Meeting(
        id=str(uuid.uuid4()),
        organizer_id=employee.id,
        title="Planning",
        start_at=datetime.now(timezone.utc) + timedelta(days=2),
        end_at=datetime.now(timezone.utc) + timedelta(days=2, minutes=30),
        join_url=f"https://example.test/meetings/{uuid.uuid4()}",
        status="scheduled",
    )
    db_session.add(meeting)
    db_session.commit()

    spoofed = client.post(
        f"/api/v1/meetings/{meeting.id}/cancel?organizer_id={employee.id}",
        headers={"X-User-Id": employee.id},
        json={},
    )
    assert spoofed.status_code == 401

    other_token = login(client, other_employee.email).json()["access_token"]
    denied = client.post(
        f"/api/v1/meetings/{meeting.id}/cancel",
        headers=bearer(other_token),
        json={},
    )
    assert denied.status_code == 403


def test_trusted_delegate_can_cancel_without_client_identity_fields(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    organizer = make_employee(db_session, "employee-1", "organizer@example.test")
    delegate = make_employee(db_session, "employee-2", "delegate@example.test")
    make_account(db_session, organizer)
    delegate_account = make_account(db_session, delegate)
    db_session.add(
        MeetingDelegate(
            id=str(uuid.uuid4()),
            organizer_id=organizer.id,
            delegate_id=delegate.id,
            meeting_id=None,
        )
    )
    meeting = Meeting(
        id=str(uuid.uuid4()),
        organizer_id=organizer.id,
        title="Planning",
        start_at=datetime.now(timezone.utc) + timedelta(days=2),
        end_at=datetime.now(timezone.utc) + timedelta(days=2, minutes=30),
        join_url=f"https://example.test/meetings/{uuid.uuid4()}",
        status="scheduled",
    )
    db_session.add(meeting)
    db_session.commit()
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: "accepted",
    )

    token = login(client, delegate_account.email_normalized).json()["access_token"]
    response = client.post(f"/api/v1/meetings/{meeting.id}/cancel", headers=bearer(token), json={})

    assert response.status_code == 200
    assert response.json()["meeting"]["status"] == "cancelled"


def test_websocket_query_ids_cannot_impersonate_organizer_or_claim_host_state(
    client: TestClient,
    db_session: Session,
):
    organizer = make_employee(db_session, "employee-1", "organizer@example.test")
    make_account(db_session, organizer)
    meeting_id = str(uuid.uuid4())
    start_at = datetime.now(timezone.utc) + timedelta(days=1)
    db_session.add(
        Meeting(
            id=meeting_id,
            organizer_id=organizer.id,
            title="Socket identity check",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            join_url=f"https://example.test/meetings/{meeting_id}",
            status="scheduled",
        )
    )
    db_session.commit()

    with client.websocket_connect(
        f"/api/v1/meetings/{meeting_id}/ws?user_id={organizer.id}&client_id={organizer.id}"
    ) as impersonator:
        impersonator_welcome = impersonator.receive_json()
        impersonator_connection = impersonator_welcome["payload"]["connection_id"]
        assert impersonator_connection != organizer.id

        with client.websocket_connect(f"/api/v1/meetings/{meeting_id}/ws") as guest:
            guest_connection = guest.receive_json()["payload"]["connection_id"]
            impersonator.receive_json()  # participant_joined
            guest.send_json({
                "type": "participant_state",
                "payload": {
                    "connection_id": organizer.id,
                    "is_organizer": True,
                    "display_name": "Forged Organizer",
                },
            })
            forged_state = impersonator.receive_json()
            assert forged_state["payload"]["connection_id"] == guest_connection
            assert forged_state["payload"]["is_organizer"] is False

            impersonator.send_json({
                "type": "mute_participant",
                "payload": {"participant_id": guest_connection},
            })
            denial = impersonator.receive_json()
            assert denial["type"] == "error"
            assert "organizer" in denial["payload"]["detail"].lower()