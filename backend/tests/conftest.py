import os

import pytest

# TestClient(app) runs the startup hook. Never let the reminder scheduler or the
# e-mail dispatcher run against the real database (and send real e-mails) during tests.
os.environ["TASK_REMINDER_SCHEDULER_ENABLED"] = "false"
os.environ["EMAIL_DISPATCHER_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _no_background_reminder_scheduler(monkeypatch):
    import main

    monkeypatch.setattr(main, "start_task_reminder_scheduler", lambda: False)
    monkeypatch.setattr(main, "start_email_dispatcher", lambda: False)


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
