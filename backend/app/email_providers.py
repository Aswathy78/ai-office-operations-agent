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

logger = logging.getLogger(__name__)


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


def send_via(settings: SmtpSettings, recipient: str, subject: str, body: str) -> str:
    """Hand one plain-text e-mail to ``settings``'s server; return its Message-ID.

    Returns only once the server accepted the message for the recipient; any
    refusal raises ``smtplib.SMTPRecipientsRefused`` instead.
    """
    if not settings.host or not settings.sender:
        raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL must be configured.")
    if bool(settings.username) != bool(settings.password):
        raise ValueError("Configure both SMTP_USERNAME and SMTP_PASSWORD.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.sender
    message["To"] = recipient
    message_id = make_msgid(domain=settings.sender.rsplit("@", 1)[-1] or None)
    message["Message-ID"] = message_id
    message.set_content(body)

    with smtplib.SMTP(settings.host, settings.port, timeout=10) as smtp:
        if settings.starttls:
            smtp.starttls()
        if settings.username:
            smtp.login(settings.username, settings.password)
        refused = smtp.send_message(message)
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
        logger.warning("Primary mail provider %s unavailable (%s); using fallback %s",
                       primary.name, primary_error, fallback.name)
        message_id = send_via(fallback, recipient, subject, body)
        return DeliveryReceipt(
            message_id,
            fallback.name,
            fallback_used=True,
            primary_error=f"{type(primary_error).__name__}: {primary_error}"[:300],
        )
