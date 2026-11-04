import os
import re

import pytest
from fastapi import HTTPException, Request


class _WhitespaceDocumentTokenizer:
    def __init__(self) -> None:
        self._token_ids: dict[str, int] = {}
        self._tokens: list[str] = []

    def encode(self, text: str) -> tuple[list[int], list[tuple[int, int]]]:
        token_ids: list[int] = []
        offsets: list[tuple[int, int]] = []
        for match in re.finditer(r"\S+", text):
            token = match.group()
            token_id = self._token_ids.get(token)
            if token_id is None:
                token_id = len(self._tokens)
                self._token_ids[token] = token_id
                self._tokens.append(token)
            token_ids.append(token_id)
            offsets.append(match.span())
        return token_ids, offsets

    def decode(self, token_ids: list[int]) -> str:
        return " ".join(self._tokens[token_id] for token_id in token_ids)

    def count(self, text: str) -> int:
        return len(self.encode(text)[0])


@pytest.fixture()
def document_test_tokenizer():
    return _WhitespaceDocumentTokenizer()


@pytest.fixture(autouse=True)
def _stub_document_embedding_tokenizer(
    request,
    monkeypatch,
    document_test_tokenizer,
):
    if request.module.__name__ not in {
        "tests.test_document_indexing",
        "tests.test_document_upload",
        "test_document_indexing",
        "test_document_upload",
    }:
        return

    import app.documents.indexing as indexing

    tokenizer = document_test_tokenizer
    monkeypatch.setattr(indexing, "count_tokens", tokenizer.count)
    monkeypatch.setattr(indexing, "encode_tokens", tokenizer.encode)


# TestClient(app) runs the startup hook. Never let the reminder scheduler or the
# e-mail dispatcher run against the real database (and send real e-mails) during tests.
os.environ["TASK_REMINDER_SCHEDULER_ENABLED"] = "false"
os.environ["EMAIL_DISPATCHER_ENABLED"] = "false"
os.environ["TASK_CALENDAR_SYNC_ENABLED"] = "false"
os.environ["TRANSCRIPTION_WORKER_ENABLED"] = "false"
os.environ["DOCUMENT_PROCESSOR_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _no_background_reminder_scheduler(monkeypatch):
    import main

    monkeypatch.setattr(main, "start_task_reminder_scheduler", lambda: False)
    monkeypatch.setattr(main, "start_email_dispatcher", lambda: False)
    monkeypatch.setattr(main, "start_task_calendar_scheduler", lambda: False)
    monkeypatch.setattr(main, "start_transcription_worker", lambda: False)
    monkeypatch.setattr(main, "start_document_processor", lambda: False)


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
            query_actor = (
                query.get("actor_id")
                or query.get("user_id")
                or query.get("organizer_id")
                or query.get("requester_id")
            )
            header_actor = request.headers.get("X-User-Id")
            if query_actor and header_actor and query_actor != header_actor:
                # Two different identities in one request can never come from one
                # signed-in user; refuse it as the real token check would.
                raise HTTPException(status_code=403, detail="Conflicting caller identity.")
            actor_id = query_actor or header_actor or "test-employee"
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
