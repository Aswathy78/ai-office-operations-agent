import smtplib

import pytest

from app import email_providers


def test_gmail_port_587_connectivity_uses_starttls_before_authentication(
    monkeypatch,
    capsys,
    caplog,
):
    events: list[tuple] = []
    password = "test-only-app-password"

    class SMTPProbe:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))
            self.esmtp_features = {"auth": "PLAIN LOGIN"}

        def has_extn(self, name):
            return name == "auth"

        def ehlo(self):
            events.append(("ehlo",))
            return 250, b"OK"

        def starttls(self):
            events.append(("starttls",))
            return 220, b"Ready"

        def login(self, username, supplied_password):
            events.append(("login", username, supplied_password))

        def auth_plain(self, _challenge=None):
            return "test"

        def auth(self, mechanism, _authobject, *, initial_response_ok=True):
            events.append(("auth", mechanism, initial_response_ok))
            return 235, b"Accepted"

        def send_message(self, _message):
            raise AssertionError("A connectivity check must not send an e-mail.")

        def close(self):
            events.append(("close",))

    def reject_ssl(*_args, **_kwargs):
        raise AssertionError("Port 587 must use SMTP with STARTTLS, not SMTP_SSL.")

    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "office@example.test")
    monkeypatch.setenv("SMTP_USERNAME", "office@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", password)
    monkeypatch.setenv("SMTP_USE_STARTTLS", "true")
    monkeypatch.delenv("FALLBACK_SMTP_HOST", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", SMTPProbe)
    monkeypatch.setattr(smtplib, "SMTP_SSL", reject_ssl)

    result = email_providers._run_connectivity_check()

    assert result == 0
    assert events == [
        ("connect", "smtp.gmail.com", 587, 10),
        ("ehlo",),
        ("starttls",),
        ("ehlo",),
        ("auth", "PLAIN", True),
        ("close",),
    ]
    output = capsys.readouterr().out
    assert output == (
        "SMTP connectivity check succeeded provider=gmail "
        "stages=EHLO:250,STARTTLS:220,EHLO after STARTTLS:250,login:235 "
        "advertised_auth=PLAIN,LOGIN auth_attempts=PLAIN auth_results=PLAIN:235\n"
    )
    assert password not in output
    assert password not in caplog.text


def test_smtp_authentication_is_rejected_when_starttls_is_disabled(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "office@example.test")
    monkeypatch.setenv("SMTP_USERNAME", "office@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "test-only-app-password")
    monkeypatch.setenv("SMTP_USE_STARTTLS", "false")
    monkeypatch.delenv("FALLBACK_SMTP_HOST", raising=False)

    with pytest.raises(ValueError, match="requires STARTTLS"):
        email_providers.send_email("recipient@example.test", "Test", "Body")


def test_failed_smtp_connectivity_check_does_not_print_credentials(
    monkeypatch,
    capsys,
    caplog,
):
    password = "test-only-app-password"

    class DisconnectedSMTP:
        def __init__(self, *_args, **_kwargs):
            self.esmtp_features = {"auth": "PLAIN LOGIN"}

        def has_extn(self, name):
            return name == "auth"

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def auth_plain(self, _challenge=None):
            return "test"

        def auth(self, _mechanism, _authobject, *, initial_response_ok=True):
            raise smtplib.SMTPServerDisconnected(password)

        def close(self):
            pass

    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "office@example.test")
    monkeypatch.setenv("SMTP_USERNAME", "office@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", password)
    monkeypatch.setenv("SMTP_USE_STARTTLS", "true")
    monkeypatch.delenv("FALLBACK_SMTP_HOST", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", DisconnectedSMTP)

    assert email_providers._run_connectivity_check() == 1

    output = capsys.readouterr().out
    assert "operation=login" in output
    assert "error_type=SMTPServerDisconnected" in output
    assert password not in output
    assert password not in caplog.text


def test_authentication_rejection_preserves_gmail_535_without_retrying_login(
    monkeypatch,
    capsys,
):
    attempted: list[str] = []

    class RejectingSMTP:
        def __init__(self, *_args, **_kwargs):
            self.esmtp_features = {"auth": "PLAIN LOGIN"}
            self.sock = object()

        def has_extn(self, name):
            return name == "auth"

        def ehlo(self):
            return 250, b"OK"

        def starttls(self):
            return 220, b"Ready"

        def auth_plain(self, _challenge=None):
            return "test"

        def auth_login(self, _challenge=None):
            return "test"

        def auth(self, mechanism, _authobject):
            attempted.append(mechanism)
            if mechanism == "PLAIN":
                raise smtplib.SMTPAuthenticationError(535, b"Rejected")
            return 235, b"Accepted"

        def close(self):
            self.sock = None

    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "office@example.test")
    monkeypatch.setenv("SMTP_USERNAME", "office@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "test-only-app-password")
    monkeypatch.setenv("SMTP_USE_STARTTLS", "true")
    monkeypatch.delenv("FALLBACK_SMTP_HOST", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", RejectingSMTP)

    assert email_providers._run_connectivity_check() == 1
    assert attempted == ["PLAIN"]
    output = capsys.readouterr().out
    assert "operation=login response_code=535" in output
    assert "auth_results=PLAIN:535" in output
    assert "error_type=SMTPAuthenticationError" in output
    assert "test-only-app-password" not in output
