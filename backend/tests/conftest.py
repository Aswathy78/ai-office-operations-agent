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


_MACHINE_SPECIFIC_ENV = ("WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE")


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
