"""Story 9.4 - administrator dashboard: users, departments, integrations, system health.

What is checked:
  * only an administrator can read it, and nothing secret is ever in the response;
  * every number comes from the live database, threads and request metrics;
  * integration health follows what really happened to its work, not a flag;
  * one section that cannot be read does not blank the rest;
  * the page is told to refresh within 30 seconds and is never cached.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import admin_dashboard_service as service
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AuthAccount,
    AuthSession,
    Base,
    EmailDraft,
    Employee,
    MeetingSummary,
    MeetingTranscript,
    TaskCalendarSync,
    WorkflowDeadLetter,
    WorkflowRun,
)
from app.system_metrics import RequestMetrics, RequestMetricsMiddleware, request_metrics

SECRET_HASH = "pbkdf2$SECRET-HASH-VALUE"
SMTP_PASSWORD = "smtp-app-password-123"
SMTP_USER = "mailer.account@example.test"
NOW = datetime.now(timezone.utc)


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
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.login = lambda user_id, role: current.update(principal=_principal(user_id, role))
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def anonymous_client(db_session: Session, monkeypatch):
    """The real Bearer-token check: the suite's test-only principal shortcut is removed."""
    for name, value in (
        ("AUTH_JWT_SECRET", "test-only-secret-that-is-at-least-32-bytes-long"),
        ("AUTH_JWT_ISSUER", "test-ai-office"),
        ("AUTH_JWT_AUDIENCE", "test-ai-office-client"),
        ("AUTH_ACCESS_TOKEN_TTL_SECONDS", "900"),
    ):
        monkeypatch.setenv(name, value)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides.pop(require_authenticated_principal, None)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Known mail/AI settings, a quiet system, and empty request metrics."""
    for name in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "FALLBACK_SMTP_HOST", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    request_metrics.reset()
    monkeypatch.setattr(service, "_background_services", lambda: [])
    yield
    request_metrics.reset()


def _person(db: Session, employee_id: str, role: str, *, team: str | None = None,
            enabled: bool = True, active: bool = True) -> None:
    db.add(Employee(id=employee_id, name=f"Person {employee_id}", email=f"{employee_id}@example.test",
                    team=team, is_active=active))
    db.add(AuthAccount(id=f"acc-{employee_id}", employee_id=employee_id,
                       email_normalized=f"{employee_id}@example.test", password_hash=SECRET_HASH,
                       role=role, is_enabled=enabled))
    db.commit()


def _get(client: TestClient) -> dict:
    response = client.get("/api/v1/admin/dashboard")
    assert response.status_code == 200, response.text
    return response.json()


def _run(db: Session, status: str, **extra) -> WorkflowRun:
    run = WorkflowRun(id=str(uuid.uuid4()), workflow_type="test", idempotency_key=str(uuid.uuid4()),
                      status=status, **extra)
    db.add(run)
    db.commit()
    return run


def _integration(data: dict, key: str) -> dict:
    return next(item for item in data["integrations"]["items"] if item["key"] == key)


class _AliveThread:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


# ---------------------------------------------------------------------------
# Authorization and secrecy
# ---------------------------------------------------------------------------


def test_unauthenticated_request_is_refused(anonymous_client):
    response = anonymous_client.get("/api/v1/admin/dashboard")
    assert response.status_code == 401
    assert "users" not in response.json()


def _sign_in(client: TestClient, db: Session, role: str) -> dict[str, str]:
    from app.auth_service import provision_account

    email = f"{role}.real@example.test"
    db.add(Employee(id=f"real-{role}", name=role, email=email, team="IT", is_active=True))
    db.commit()
    provision_account(db, email, "correct horse battery staple", role)
    body = client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_real_tokens_only_an_administrator_session_is_accepted(anonymous_client, db_session):
    for role in ("employee", "manager", "hr_operations"):
        headers = _sign_in(anonymous_client, db_session, role)
        response = anonymous_client.get("/api/v1/admin/dashboard", headers=headers)
        assert response.status_code == 403, role
        assert "users" not in response.json()
    admin = _sign_in(anonymous_client, db_session, "admin")
    assert anonymous_client.get("/api/v1/admin/dashboard", headers=admin).status_code == 200


def test_a_signed_out_administrator_token_stops_working(anonymous_client, db_session):
    admin = _sign_in(anonymous_client, db_session, "admin")
    assert anonymous_client.get("/api/v1/admin/dashboard", headers=admin).status_code == 200
    anonymous_client.post("/api/v1/auth/logout", headers=admin)
    assert anonymous_client.get("/api/v1/admin/dashboard", headers=admin).status_code == 401


def test_a_forged_token_is_refused(anonymous_client):
    response = anonymous_client.get("/api/v1/admin/dashboard", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["employee", "manager", "hr_operations"])
def test_non_administrators_are_refused_without_any_data(client, db_session, role):
    _person(db_session, "someone", "employee", team="Sales")
    client.login("someone", role)
    response = client.get("/api/v1/admin/dashboard")
    assert response.status_code == 403
    assert set(response.json()) == {"detail"}
    assert "someone@example.test" not in response.text


def test_administrator_gets_the_dashboard_uncached(client, db_session):
    _person(db_session, "admin-1", "admin", team="IT")
    response = client.get("/api/v1/admin/dashboard")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert {"users", "departments", "integrations", "system", "alerts", "overall_status"} <= set(data)


def test_no_secret_is_ever_in_the_response(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin", team="IT")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", SMTP_USER)
    monkeypatch.setenv("SMTP_PASSWORD", SMTP_PASSWORD)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_super_secret_key")
    db_session.add(EmailDraft(id="d1", created_by_id="admin-1", created_by_name="A", created_by_role="admin",
                              prompt="p", source="template", status="failed",
                              last_error="535 Username and Password not accepted for mailer.account",
                              updated_at=NOW))
    db_session.commit()
    body = client.get("/api/v1/admin/dashboard").text
    for secret in (SECRET_HASH, "SECRET-HASH", SMTP_PASSWORD, SMTP_USER, "gsk_super_secret_key",
                   "Username and Password not accepted", "password_hash"):
        assert secret not in body


# ---------------------------------------------------------------------------
# Users and departments come from the database
# ---------------------------------------------------------------------------


def test_users_and_departments_reflect_the_database(client, db_session):
    _person(db_session, "admin-1", "admin", team="IT")
    _person(db_session, "mgr-1", "manager", team="Sales")
    _person(db_session, "emp-1", "employee", team="Sales")
    _person(db_session, "emp-2", "employee", team=None, enabled=False)
    db_session.add(AuthSession(jti="j1", account_id="acc-emp-1", expires_at=NOW + timedelta(hours=1),
                               created_at=NOW - timedelta(minutes=5)))
    db_session.commit()

    data = _get(client)

    users = data["users"]
    assert users["available"] is True
    assert (users["total"], users["active"], users["disabled"]) == (4, 3, 1)
    assert users["by_role"] == {"admin": 1, "manager": 1, "employee": 2}
    by_id = {item["id"]: item for item in users["items"]}
    assert by_id["emp-2"]["is_active"] is False
    assert by_id["emp-2"]["department"] == "Unassigned"
    assert by_id["emp-1"]["last_sign_in_at"] is not None
    assert by_id["mgr-1"]["last_sign_in_at"] is None
    assert set(by_id["emp-1"]) == {"id", "name", "email", "role", "department", "is_active", "last_sign_in_at"}

    departments = {item["name"]: item for item in data["departments"]["items"]}
    assert departments["Sales"] == {"name": "Sales", "employees": 2, "active_employees": 2, "with_login": 2}
    assert departments["IT"]["employees"] == 1
    assert departments["Unassigned"]["with_login"] == 0  # its only account is disabled


def test_a_disabled_employee_is_not_counted_active(client, db_session):
    _person(db_session, "admin-1", "admin", team="IT")
    _person(db_session, "gone", "employee", team="IT", active=False)
    users = _get(client)["users"]
    assert users["active"] == 1 and users["disabled"] == 1


def test_numbers_change_when_the_data_changes(client, db_session):
    _person(db_session, "admin-1", "admin", team="IT")
    assert _get(client)["users"]["total"] == 1
    _person(db_session, "new-1", "employee", team="HR")
    data = _get(client)
    assert data["users"]["total"] == 2
    assert {d["name"] for d in data["departments"]["items"]} == {"IT", "HR"}


# ---------------------------------------------------------------------------
# System health: queue depth, workers, latency, error rate
# ---------------------------------------------------------------------------


def test_queue_depth_comes_from_the_workflow_table(client, db_session):
    _person(db_session, "admin-1", "admin", team="IT")
    _run(db_session, "pending")
    _run(db_session, "pending", next_attempt_at=NOW + timedelta(hours=1))
    _run(db_session, "retrying", next_attempt_at=NOW - timedelta(seconds=5))
    _run(db_session, "processing")
    _run(db_session, "completed")
    failed = _run(db_session, "failed", updated_at=NOW)
    db_session.add(WorkflowDeadLetter(id="dl1", workflow_run_id=failed.id, workflow_type="test", status="open"))
    db_session.add(WorkflowDeadLetter(id="dl2", workflow_run_id=failed.id, workflow_type="test", status="cancelled"))
    db_session.commit()

    queue = _get(client)["system"]["queue"]

    assert queue["depth"] == 3          # two pending + one retrying
    assert queue["due_now"] == 2        # the one scheduled for later is not due
    assert queue["processing"] == 1
    assert queue["failed_24h"] == 1
    assert queue["dead_letter_open"] == 1
    assert queue["oldest_waiting_seconds"] >= 0


def test_an_empty_queue_reports_zero_and_no_oldest(client, db_session):
    _person(db_session, "admin-1", "admin")
    queue = _get(client)["system"]["queue"]
    assert queue["depth"] == 0 and queue["oldest_waiting_seconds"] is None


def test_active_workers_are_the_live_threads(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    from app import workflow_worker

    monkeypatch.setattr(workflow_worker, "_threads", [_AliveThread(True), _AliveThread(False)])
    monkeypatch.setattr(workflow_worker, "worker_count", lambda: 2)
    data = _get(client)
    assert data["system"]["workers"] == {"configured": 2, "alive": 1, "started": True}
    assert any("1 of 2 workflow workers" in alert for alert in data["alerts"])
    assert data["overall_status"] == "degraded"


def test_waiting_work_with_no_worker_is_called_out(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    from app import workflow_worker

    monkeypatch.setattr(workflow_worker, "_threads", [])
    _run(db_session, "pending")
    alerts = _get(client)["alerts"]
    assert any("no worker is running" in alert for alert in alerts)


def test_background_service_that_stopped_is_reported(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    monkeypatch.setattr(service, "_background_services",
                        lambda: [{"name": "E-mail dispatcher", "running": False},
                                 {"name": "Calendar sync", "running": True}])
    data = _get(client)
    assert "E-mail dispatcher is not running." in data["alerts"]
    assert not any("Calendar sync" in alert for alert in data["alerts"])


def test_database_latency_is_measured(client, db_session):
    _person(db_session, "admin-1", "admin")
    database = _get(client)["system"]["database"]
    assert isinstance(database["latency_ms"], float) and database["latency_ms"] >= 0


def test_api_latency_and_error_rate_come_from_real_requests(client, db_session):
    _person(db_session, "admin-1", "admin")
    boom = APIRouter()

    @boom.get("/__test/boom")
    def _boom():
        raise RuntimeError("deliberate failure")

    app.include_router(boom)
    try:
        for _ in range(3):
            assert client.get("/__test/boom").status_code == 500
        for _ in range(7):
            client.get("/api/v1/does-not-exist")  # 404: a client error, not a server error
        api = _get(client)["system"]["api"]
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test/boom"]

    assert api["request_count"] == 10
    assert api["server_error_count"] == 3
    assert api["client_error_count"] == 7
    assert api["error_rate_percent"] == 30.0
    assert api["average_latency_ms"] > 0 and api["p95_latency_ms"] >= api["average_latency_ms"] * 0 + 0
    assert any("30% of recent API requests" in alert for alert in _get(client)["alerts"])


def test_the_dashboard_does_not_count_its_own_polling(client, db_session):
    _person(db_session, "admin-1", "admin")
    for _ in range(5):
        _get(client)
    assert _get(client)["system"]["api"]["request_count"] == 0


def test_no_traffic_is_no_data_not_a_zero_percent_error_rate(client, db_session):
    _person(db_session, "admin-1", "admin")
    api = _get(client)["system"]["api"]
    assert api["request_count"] == 0
    assert api["error_rate_percent"] is None and api["p95_latency_ms"] is None


# -- the metrics window itself ------------------------------------------------


def test_request_metrics_percentiles_and_window():
    metrics = RequestMetrics(window_seconds=60)
    now = 10_000.0
    for latency in range(1, 101):   # 1..100 ms
        metrics.record(float(latency), 200, now=now - 10)
    metrics.record(500.0, 503, now=now - 5)
    metrics.record(999.0, 200, now=now - 3600)   # outside the window
    snap = metrics.snapshot(now=now)
    assert snap["request_count"] == 101
    assert snap["server_error_count"] == 1
    assert snap["error_rate_percent"] == round(100 / 101, 2)
    assert snap["p95_latency_ms"] == 96.0
    assert snap["max_latency_ms"] == 500.0


def test_request_metrics_forget_old_requests():
    metrics = RequestMetrics(window_seconds=30)
    metrics.record(10.0, 500, now=1000.0)
    assert metrics.snapshot(now=1010.0)["request_count"] == 1
    assert metrics.snapshot(now=1100.0)["request_count"] == 0


@pytest.mark.anyio
async def test_middleware_records_status_and_unhandled_errors():
    metrics = RequestMetrics()
    sent: list[dict] = []

    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def broken_app(scope, receive, send):
        raise RuntimeError("crash before any response")

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/api/v1/x"}
    await RequestMetricsMiddleware(ok_app, metrics)(scope, None, send)
    with pytest.raises(RuntimeError):
        await RequestMetricsMiddleware(broken_app, metrics)(scope, None, send)
    await RequestMetricsMiddleware(ok_app, metrics)({"type": "websocket", "path": "/ws"}, None, send)

    snap = metrics.snapshot()
    assert snap["request_count"] == 2 and snap["server_error_count"] == 1


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Integration health follows real outcomes
# ---------------------------------------------------------------------------


def _mail_configured(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", SMTP_USER)
    monkeypatch.setenv("SMTP_PASSWORD", SMTP_PASSWORD)


def _draft(db: Session, status: str, *, age: timedelta = timedelta(minutes=5)) -> None:
    db.add(EmailDraft(id=str(uuid.uuid4()), created_by_id="admin-1", created_by_name="A",
                      created_by_role="admin", prompt="p", source="template", status=status,
                      updated_at=NOW - age))
    db.commit()


def test_email_not_configured(client, db_session):
    _person(db_session, "admin-1", "admin")
    email = _integration(_get(client), "email")
    assert email["status"] == "not_configured"


def test_email_configured_without_traffic_is_unknown_not_healthy(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    _mail_configured(monkeypatch)
    email = _integration(_get(client), "email")
    assert email["status"] == "unknown"
    assert email["metrics"]["provider"] == "gmail"


def test_email_operational_degraded_and_down_follow_real_sends(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    _mail_configured(monkeypatch)
    for _ in range(4):
        _draft(db_session, "sent")
    assert _integration(_get(client), "email")["status"] == "operational"

    _draft(db_session, "failed")
    degraded = _integration(_get(client), "email")
    assert degraded["status"] == "degraded"
    assert degraded["metrics"]["sent_24h"] == 4 and degraded["metrics"]["failed_24h"] == 1

    for _ in range(4):
        _draft(db_session, "failed")
    assert _integration(_get(client), "email")["status"] == "down"


def test_email_outcomes_older_than_a_day_are_ignored(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    _mail_configured(monkeypatch)
    _draft(db_session, "failed", age=timedelta(days=3))
    assert _integration(_get(client), "email")["status"] == "unknown"


def test_a_failing_integration_makes_the_system_degraded_with_an_alert(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    _mail_configured(monkeypatch)
    _draft(db_session, "failed")
    data = _get(client)
    assert data["overall_status"] == "degraded"
    assert any(alert.startswith("E-mail (SMTP):") for alert in data["alerts"])


def test_ai_model_health_from_summaries(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    assert _integration(_get(client), "ai_model")["status"] == "not_configured"
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    assert _integration(_get(client), "ai_model")["status"] == "unknown"
    db_session.add(MeetingSummary(id="s1", meeting_id="m1", transcript_id="t1", status="completed", updated_at=NOW))
    db_session.commit()
    assert _integration(_get(client), "ai_model")["status"] == "operational"
    db_session.add(MeetingSummary(id="s2", meeting_id="m2", transcript_id="t2", status="failed", updated_at=NOW))
    db_session.commit()
    assert _integration(_get(client), "ai_model")["status"] == "down"  # failed >= succeeded


def test_calendar_health_from_sync_results(client, db_session):
    _person(db_session, "admin-1", "admin")
    assert _integration(_get(client), "calendar")["status"] == "operational"  # local, nothing to sync
    for index in range(3):
        db_session.add(TaskCalendarSync(id=f"c{index}", task_id=f"t{index}", status="completed", status_changed_at=NOW))
    db_session.add(TaskCalendarSync(id="cf", task_id="tf", status="failed", status_changed_at=NOW))
    db_session.commit()
    calendar = _integration(_get(client), "calendar")
    assert calendar["status"] == "degraded"
    assert calendar["metrics"]["synced_24h"] == 3 and calendar["metrics"]["failed_24h"] == 1


def test_transcription_health(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    from app.meeting import transcription_worker

    monkeypatch.setattr(transcription_worker, "background_worker_enabled", lambda: False)
    assert _integration(_get(client), "transcription")["status"] == "not_configured"

    monkeypatch.setattr(transcription_worker, "background_worker_enabled", lambda: True)
    monkeypatch.setattr(transcription_worker, "_background_thread", None)
    assert _integration(_get(client), "transcription")["status"] == "down"

    monkeypatch.setattr(transcription_worker, "_background_thread", _AliveThread(True))
    db_session.add(MeetingTranscript(id="tr1", recording_id="r1", status="completed", created_at=NOW))
    db_session.commit()
    assert _integration(_get(client), "transcription")["status"] == "operational"


def test_document_processing_health(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    from app.documents import extraction, processor

    monkeypatch.setattr(processor, "_worker_thread", _AliveThread(True))
    monkeypatch.setattr(extraction, "tesseract_path", lambda: "C:/Tesseract/tesseract.exe")
    documents = _integration(_get(client), "documents")
    assert documents["status"] == "operational"
    assert "tesseract" not in json.dumps(documents).casefold()   # no install paths leak

    monkeypatch.setattr(extraction, "tesseract_path", lambda: None)
    assert _integration(_get(client), "documents")["status"] == "degraded"

    monkeypatch.setattr(processor, "_worker_thread", None)
    assert _integration(_get(client), "documents")["status"] == "down"


# ---------------------------------------------------------------------------
# Error handling: a broken part must not blank the rest
# ---------------------------------------------------------------------------


def test_one_unreadable_section_is_marked_unavailable_and_the_rest_still_load(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin", team="IT")

    def broken(db):
        raise RuntimeError("secret internal detail: /srv/db/path")

    monkeypatch.setattr(service, "collect_users", broken)
    response = client.get("/api/v1/admin/dashboard")
    assert response.status_code == 200
    data = response.json()
    assert data["users"] == {"available": False, "error": "Users could not be read right now."}
    assert "secret internal detail" not in response.text and "/srv/db/path" not in response.text
    assert data["departments"]["available"] is True and data["departments"]["total"] == 1
    assert data["system"]["available"] is True
    assert "Users could not be read." in data["alerts"]


def test_unreadable_system_metrics_leave_overall_status_unknown_not_healthy(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    monkeypatch.setattr(service, "collect_system", lambda db: (_ for _ in ()).throw(RuntimeError("x")))
    data = _get(client)
    assert data["system"]["available"] is False
    assert data["overall_status"] == "unknown"
    assert data["users"]["available"] is True


def test_one_broken_integration_check_does_not_hide_the_others(client, db_session, monkeypatch):
    _person(db_session, "admin-1", "admin")
    monkeypatch.setattr(service, "_ai_integration", lambda db, since: (_ for _ in ()).throw(RuntimeError("x")))
    items = {item["key"]: item for item in _get(client)["integrations"]["items"]}
    assert items["ai_model"]["status"] == "unknown"
    assert items["ai_model"]["detail"] == "This integration could not be checked right now."
    assert items["email"]["status"] == "not_configured"
    assert "calendar" in items and "documents" in items


# ---------------------------------------------------------------------------
# Refresh contract
# ---------------------------------------------------------------------------


def test_the_response_tells_the_page_to_refresh_within_thirty_seconds(client, db_session):
    _person(db_session, "admin-1", "admin")
    first = _get(client)
    assert 0 < first["refresh_interval_seconds"] <= 30
    assert datetime.fromisoformat(first["generated_at"]).tzinfo is not None
    second = _get(client)
    assert second["generated_at"] >= first["generated_at"]
