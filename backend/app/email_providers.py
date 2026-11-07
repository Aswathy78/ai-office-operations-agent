"""Mail providers with fallback (Story 4.6 / FR-23).

The primary provider is the existing ``SMTP_*`` configuration (smtp.gmail.com in
this project). When it is unavailable — cannot connect, times out, rejects the
login, defers with a 4xx reply, or is not configured — the same message is
handed to the fallback provider configured with ``FALLBACK_SMTP_*``. Any
SMTP relay works; Amazon SES is used through its SMTP interface, e.g.::

    FALLBACK_SMTP_HOST=email-smtp.us-east-1.amazonaws.com
    FALLBACK_SMTP_PORT=587
    FALLBACK_SMTP_USERNAME=<SES SMTP user>
    FALLBACK_SMTP_PASSWORD=<SES SMTP password>
    FALLBACK_SMTP_FROM_EMAIL=<verified sender>   # defaults to SMTP_FROM_EMAIL

A 5xx rejection of the recipient or message is *not* a provider outage; it is
returned to the caller (and recorded as a bounce) without trying the fallback.

A message counts as sent only when a provider accepted it for the recipient.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
import logging
import os
import smtplib
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)
_Result = TypeVar("_Result")
_UNSUPPORTED_AUTH_CODES = frozenset({500, 502, 504})


@dataclass(frozen=True)
class SmtpSettings:
    name: str
    host: str
    port: int
    sender: str
    username: str
    password: str
    starttls: bool


@dataclass(frozen=True)
class DeliveryReceipt:
    """Proof that a provider accepted the message for one recipient."""

    message_id: str
    provider: str
    fallback_used: bool = False
    primary_error: str | None = None


def _provider_name(host: str) -> str:
    host = host.casefold()
    if "gmail" in host or "google" in host:
        return "gmail"
    if "amazonaws" in host:
        return "ses"
    if "office365" in host or "outlook" in host:
        return "microsoft"
    return host or "smtp"


def _settings(prefix: str, default_sender: str = "") -> SmtpSettings | None:
    from app.env_refresh import refresh_mail_settings

    refresh_mail_settings()  # an edit to backend/.env takes effect without a restart
    host = os.getenv(f"{prefix}SMTP_HOST", "").strip()
    sender = os.getenv(f"{prefix}SMTP_FROM_EMAIL", "").strip() or default_sender
    if not host and not prefix:
        # Keep the primary visible so callers get a clear "not configured" error.
        return SmtpSettings("smtp", "", 587, sender, "", "", True)
    if not host:
        return None
    try:
        port = int(os.getenv(f"{prefix}SMTP_PORT", "587"))
    except ValueError as exc:
        raise ValueError(f"{prefix}SMTP_PORT must be an integer.") from exc
    return SmtpSettings(
        name=_provider_name(host),
        host=host,
        port=port,
        sender=sender,
        username=os.getenv(f"{prefix}SMTP_USERNAME", ""),
        password=os.getenv(f"{prefix}SMTP_PASSWORD", ""),
        starttls=os.getenv(f"{prefix}SMTP_USE_STARTTLS", "true").strip().lower()
        not in {"0", "false", "no"},
    )


def primary_settings() -> SmtpSettings:
    return _settings("")  # type: ignore[return-value]


def fallback_settings() -> SmtpSettings | None:
    return _settings("FALLBACK_", default_sender=os.getenv("SMTP_FROM_EMAIL", "").strip())


def _smtp_operation(
    provider: str,
    operation: str,
    call: Callable[[], _Result],
) -> _Result:
    try:
        return call()
    except (OSError, smtplib.SMTPException) as exc:
        setattr(exc, "smtp_operation", operation)
        if isinstance(exc, smtplib.SMTPResponseException):
            setattr(exc, "smtp_response_code", exc.smtp_code)
        logger.warning(
            "SMTP delivery operation failed provider=%s operation=%s response_code=%s error_type=%s",
            provider,
            operation,
            getattr(exc, "smtp_response_code", None),
            type(exc).__name__,
        )
        raise


def _close_smtp_connection(smtp: smtplib.SMTP, provider: str) -> None:
    try:
        smtp.close()
    except OSError as exc:
        logger.warning(
            "SMTP connection cleanup failed provider=%s error_type=%s",
            provider,
            type(exc).__name__,
        )


def _smtp_login(
    smtp: smtplib.SMTP,
    username: str,
    password: str,
    *,
    auth_attempts: list[str] | None = None,
    auth_results: list[str] | None = None,
) -> tuple[int, bytes]:
    if not smtp.has_extn("auth"):
        raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")

    advertised = set(smtp.esmtp_features["auth"].upper().split())
    supported = [
        mechanism
        for mechanism in ("CRAM-MD5", "PLAIN", "LOGIN")
        if mechanism in advertised
        and hasattr(smtp, f"auth_{mechanism.lower().replace('-', '_')}")
    ]
    if not supported:
        raise smtplib.SMTPException("No suitable SMTP authentication method was advertised.")

    smtp.user = username
    smtp.password = password
    last_error: smtplib.SMTPAuthenticationError | None = None
    for mechanism in supported:
        if auth_attempts is not None:
            auth_attempts.append(mechanism)
        try:
            response = smtp.auth(
                mechanism,
                getattr(smtp, f"auth_{mechanism.lower().replace('-', '_')}"),
            )
        except smtplib.SMTPAuthenticationError as exc:
            if auth_results is not None:
                auth_results.append(f"{mechanism}:{exc.smtp_code}")
            if exc.smtp_code not in _UNSUPPORTED_AUTH_CODES:
                raise
            last_error = exc
            continue
        except smtplib.SMTPNotSupportedError:
            if auth_results is not None:
                auth_results.append(f"{mechanism}:unsupported")
            continue
        except smtplib.SMTPServerDisconnected:
            if auth_results is not None:
                auth_results.append(f"{mechanism}:disconnected")
            raise

        code = response[0]
        if auth_results is not None:
            auth_results.append(f"{mechanism}:{code}")
        if code in (235, 503):
            return response
        raise smtplib.SMTPAuthenticationError(code, response[1])

    if last_error is not None:
        raise last_error
    raise smtplib.SMTPException("No supported SMTP authentication method succeeded.")


def _open_smtp_connection(
    settings: SmtpSettings,
    *,
    capture_auth_methods: bool = False,
) -> smtplib.SMTP:
    if not settings.host or not settings.sender:
        raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL must be configured.")
    if bool(settings.username) != bool(settings.password):
        raise ValueError("Configure both SMTP_USERNAME and SMTP_PASSWORD.")
    if settings.username and not settings.starttls:
        raise ValueError("SMTP authentication requires STARTTLS.")

    smtp = _smtp_operation(
        settings.name,
        "connect",
        lambda: smtplib.SMTP(settings.host, settings.port, timeout=10),
    )
    trace: list[str] = []
    auth_methods: list[str] = []
    auth_results: list[str] = []
    advertised_auth_methods: tuple[str, ...] = ()

    def run(operation: str, call: Callable[[], _Result]) -> _Result:
        response = _smtp_operation(settings.name, operation, call)
        code = response[0] if isinstance(response, tuple) and response else None
        trace.append(f"{operation}:{code if isinstance(code, int) else 'ok'}")
        logger.info(
            "SMTP operation succeeded provider=%s operation=%s response_code=%s",
            settings.name,
            operation,
            code if isinstance(code, int) else None,
        )
        return response

    try:
        run("EHLO", smtp.ehlo)
        if settings.starttls:
            run("STARTTLS", smtp.starttls)
            run("EHLO after STARTTLS", smtp.ehlo)
        if settings.username:
            advertised_auth_methods = tuple(
                smtp.esmtp_features.get("auth", "").upper().split()
            )
            run(
                "login",
                lambda: _smtp_login(
                    smtp,
                    settings.username,
                    settings.password,
                    auth_attempts=auth_methods if capture_auth_methods else None,
                    auth_results=auth_results if capture_auth_methods else None,
                ),
            )
    except (OSError, smtplib.SMTPException) as exc:
        setattr(exc, "smtp_trace", tuple(trace))
        setattr(exc, "smtp_connection_open", getattr(smtp, "sock", None) is not None)
        setattr(exc, "smtp_advertised_auth_methods", advertised_auth_methods)
        setattr(exc, "smtp_auth_attempts", tuple(auth_methods))
        setattr(exc, "smtp_auth_results", tuple(auth_results))
        _close_smtp_connection(smtp, settings.name)
        raise
    setattr(smtp, "smtp_trace", tuple(trace))
    setattr(smtp, "smtp_advertised_auth_methods", advertised_auth_methods)
    setattr(smtp, "smtp_auth_attempts", tuple(auth_methods))
    setattr(smtp, "smtp_auth_results", tuple(auth_results))
    return smtp


def check_smtp_connection(
    settings: SmtpSettings | None = None,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Verify SMTP connection, TLS, and configured login without sending a message."""
    settings = settings or primary_settings()
    smtp = _open_smtp_connection(settings, capture_auth_methods=True)
    trace = getattr(smtp, "smtp_trace", ())
    advertised_auth_methods = getattr(smtp, "smtp_advertised_auth_methods", ())
    auth_attempts = getattr(smtp, "smtp_auth_attempts", ())
    auth_results = getattr(smtp, "smtp_auth_results", ())
    _close_smtp_connection(smtp, settings.name)
    return settings.name, trace, advertised_auth_methods, auth_attempts, auth_results


def send_via(settings: SmtpSettings, recipient: str, subject: str, body: str) -> str:
    """Hand one plain-text e-mail to ``settings``'s server; return its Message-ID.

    Returns only once the server accepted the message for the recipient; any
    refusal raises ``smtplib.SMTPRecipientsRefused`` instead.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.sender
    message["To"] = recipient
    message_id = make_msgid(domain=settings.sender.rsplit("@", 1)[-1] or None)
    message["Message-ID"] = message_id
    message.set_content(body)

    smtp = _open_smtp_connection(settings)
    try:
        refused = _smtp_operation(
            settings.name,
            "sendmail/response handling",
            lambda: smtp.send_message(message),
        )
    finally:
        # A context manager sends QUIT; a disconnect during that cleanup can
        # falsely turn an accepted message into a retry and send it twice.
        _close_smtp_connection(smtp, settings.name)
    if refused:
        # The server answered but did not accept this recipient.
        raise smtplib.SMTPRecipientsRefused(refused)
    return message_id


def smtp_code(error: Exception) -> int | None:
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in error.recipients.values()]
        return max(codes) if codes else None
    if isinstance(error, smtplib.SMTPResponseException):
        return error.smtp_code
    return None


def is_provider_unavailable(error: Exception) -> bool:
    """The provider itself could not take the message (worth trying the fallback)."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return True  # broken credentials: this provider cannot send right now
    code = smtp_code(error)
    if code is not None:
        return 400 <= code < 500
    if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                          smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError)):
        return True
    if isinstance(error, ValueError):
        return True  # not configured
    return isinstance(error, OSError)  # refused connection, DNS, timeout


def send_email(recipient: str, subject: str, body: str) -> DeliveryReceipt:
    """Deliver through the primary provider, falling back when it is unavailable."""
    primary = primary_settings()
    try:
        return DeliveryReceipt(send_via(primary, recipient, subject, body), primary.name)
    except (OSError, smtplib.SMTPException, ValueError) as primary_error:
        fallback = fallback_settings()
        if fallback is None or not is_provider_unavailable(primary_error):
            raise
        logger.warning(
            "Primary mail provider unavailable provider=%s error_type=%s fallback=%s",
            primary.name,
            type(primary_error).__name__,
            fallback.name,
        )
        message_id = send_via(fallback, recipient, subject, body)
        return DeliveryReceipt(
            message_id,
            fallback.name,
            fallback_used=True,
            primary_error=f"{type(primary_error).__name__}: {primary_error}"[:300],
        )


def _run_connectivity_check() -> int:
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(
        dotenv_path=Path(__file__).resolve().parents[1] / ".env",
        override=False,
    )
    settings = primary_settings()
    try:
        provider, trace, advertised_auth, auth_attempts, auth_results = check_smtp_connection(
            settings
        )
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        logger.warning(
            "SMTP connectivity check failed provider=%s operation=%s error_type=%s",
            settings.name,
            getattr(exc, "smtp_operation", "configuration"),
            type(exc).__name__,
        )
        print(
            "SMTP connectivity check failed "
            f"provider={settings.name} "
            f"operation={getattr(exc, 'smtp_operation', 'configuration')} "
            f"response_code={getattr(exc, 'smtp_response_code', None)} "
            f"connection_open={getattr(exc, 'smtp_connection_open', None)} "
            f"completed_stages={','.join(getattr(exc, 'smtp_trace', ())) or 'none'} "
            f"advertised_auth={','.join(getattr(exc, 'smtp_advertised_auth_methods', ())) or 'none'} "
            f"auth_attempts={','.join(getattr(exc, 'smtp_auth_attempts', ())) or 'none'} "
            f"auth_results={','.join(getattr(exc, 'smtp_auth_results', ())) or 'none'} "
            f"error_type={type(exc).__name__}"
        )
        return 1
    print(
        "SMTP connectivity check succeeded "
        f"provider={provider} "
        f"stages={','.join(trace) or 'none'} "
        f"advertised_auth={','.join(advertised_auth) or 'none'} "
        f"auth_attempts={','.join(auth_attempts) or 'none'} "
        f"auth_results={','.join(auth_results) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_connectivity_check())
