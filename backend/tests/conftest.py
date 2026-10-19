import os

import pytest
from fastapi import Request

# TestClient(app) runs the startup hook. Never let the reminder scheduler or the
# e-mail dispatcher run against the real database (and send real e-mails) during tests.
os.environ["TASK_REMINDER_SCHEDULER_ENABLED"] = "false"
os.environ["EMAIL_DISPATCHER_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _no_background_reminder_scheduler(monkeypatch):
    import main

    monkeypatch.setattr(main, "start_task_reminder_scheduler", lambda: False)
    monkeypatch.setattr(main, "start_email_dispatcher", lambda: False)


@pytest.fixture(autouse=True)
def _test_authentication_boundary(request):
    """Adapt legacy query-actor tests to a test-only authenticated principal.

    Authentication tests deliberately use the real Bearer-token dependencies.
    This adapter is test-only and is never installed by application code.
    """
    import main
    from app.auth_service import AuthenticatedPrincipal
    from app.authentication import (
        authenticated_actor_id,
        require_authenticated_actor_id,
        require_authenticated_principal,
    )

    original_overrides = main.app.dependency_overrides.copy()
    startup_handlers = main.app.router.on_startup[:]
    shutdown_handlers = main.app.router.on_shutdown[:]
    main.app.router.on_startup.clear()
    main.app.router.on_shutdown.clear()

    if request.module.__name__.endswith("test_authentication"):
        principal_overrides = {}
    else:
        def principal_from_legacy_test_request(request: Request) -> AuthenticatedPrincipal:
            query = request.query_params
            actor_id = (
                query.get("actor_id")
                or query.get("user_id")
                or query.get("organizer_id")
                or query.get("requester_id")
                or request.headers.get("X-User-Id")
                or "test-employee"
            )
            role = query.get("actor_role", "employee")
            team = query.get("actor_team")
            email = (
                query.get("email")
                or query.get("employee_email")
                or query.get("actor_email")
                or f"{actor_id}@test.invalid"
            )
            return AuthenticatedPrincipal(
                user_id=actor_id,
                account_id="test-auth-account",
                email=email,
                name="Test Actor",
                role=role,
                team=team,
                jti="test-auth-session",
            )

        def principal_dependency(request: Request) -> AuthenticatedPrincipal:
            return principal_from_legacy_test_request(request)

        def actor_dependency(request: Request) -> str:
            return principal_from_legacy_test_request(request).user_id

        principal_overrides = {
            require_authenticated_principal: principal_dependency,
            require_authenticated_actor_id: actor_dependency,
            authenticated_actor_id: actor_dependency,
        }
        main.app.dependency_overrides.update(principal_overrides)

    try:
        yield
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(original_overrides)
        main.app.router.on_startup[:] = startup_handlers
        main.app.router.on_shutdown[:] = shutdown_handlers


class _NoRealSmtp:
    """Stands in for smtplib.SMTP so no test can ever reach a real mail server."""

    def __init__(self, *args, **kwargs):
        raise ConnectionRefusedError(
            "Real SMTP is disabled in tests; patch app.email_providers.send_email or smtplib.SMTP."
        )


@pytest.fixture(autouse=True)
def _block_real_smtp(monkeypatch):
    # backend/.env holds the real Gmail SMTP settings and the app's startup loads it;
    # a test that forgets to fake the provider must fail, not send real e-mail.
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _NoRealSmtp)


_MACHINE_SPECIFIC_ENV = (
    "WHISPER_DEVICE",
    "WHISPER_COMPUTE_TYPE",
    # Delivery settings a developer may set locally; tests use the documented defaults.
    "FALLBACK_SMTP_HOST",
    "SYSTEM_ADMIN_EMAILS",
    "EMAIL_BOUNCE_WEBHOOK_TOKEN",
    "EMAIL_MAX_ATTEMPTS",
    "EMAIL_RETRY_BACKOFF_MINUTES",
)


@pytest.fixture(autouse=True)
def _default_whisper_settings(monkeypatch):
    # backend/.env may select CPU Whisper for this machine, and the app's startup
    # hook (run by TestClient) copies .env into os.environ. Tests assert the
    # documented defaults, so clear these both now and after every .env load.
    import main

    for name in _MACHINE_SPECIFIC_ENV:
        monkeypatch.delenv(name, raising=False)

    real_load_dotenv = main.load_dotenv

    def load_dotenv_without_machine_settings(*args, **kwargs):
        result = real_load_dotenv(*args, **kwargs)
        for name in _MACHINE_SPECIFIC_ENV:
            os.environ.pop(name, None)
        return result

    monkeypatch.setattr(main, "load_dotenv", load_dotenv_without_machine_settings)
