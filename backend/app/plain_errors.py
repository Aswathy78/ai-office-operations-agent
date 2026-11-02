"""Plain-language wording for technical failures that people see.

A failed e-mail used to be shown as ``SMTPAuthenticationError: (535, b'5.7.8 Username
and Password not accepted...')``. Nobody who reads a notification or a task can act on
that. This turns a failure into a sentence that says what happened and, when someone
must act, who. The technical detail is not lost: callers log it for administrators.

Two ways in:

* an exception object, where the *type* decides the wording (so an ordinary message
  such as "provider timed out" is never mistaken for a mail problem), and
* text already stored by an earlier version, which is only reworded when it carries a
  technical signature (``SMTPAuthenticationError: (535, b'...')``, ``OSError: ...``).

Anything already readable is returned unchanged, so applying it twice is harmless.
"""

from __future__ import annotations

import re
import smtplib
import socket
import ssl
from typing import Annotated

from pydantic import BeforeValidator

GENERIC = (
    "Something went wrong and the action could not be completed. An administrator "
    "can find the technical details in the server log."
)
SIGN_IN_REJECTED = (
    "The e-mail account sign-in was rejected, so the message could not be sent. Ask an "
    "administrator to check the e-mail username and app password in the server settings."
)
RECIPIENT_REFUSED = (
    "The mail server refused the recipient's address. Check that the address is correct."
)
RECIPIENT_LATER = (
    "The recipient's mail server could not accept the message right now. It will be tried "
    "again automatically."
)
SIGN_IN_BLOCKED = (
    "The mail server hung up while signing in. This usually means Google has temporarily "
    "blocked sign-ins after too many failed attempts. Wait an hour and try again; if it keeps "
    "happening, ask an administrator to check the e-mail app password."
)
SENDER_REFUSED = (
    "The mail server refused the sender address this system uses. Ask an administrator to "
    "check the sender address in the e-mail settings."
)
UNREACHABLE = (
    "The mail server could not be reached. This is usually temporary, so it will be tried "
    "again automatically."
)
BUSY = (
    "The mail server is busy or limiting messages right now. It will be tried again "
    "automatically."
)
TOO_LARGE = "The mail server rejected the message because it is too large."
REJECTED = (
    "The mail server rejected the message. Ask an administrator to check the server log "
    "for the reason."
)
MAIL_SETUP = (
    "E-mail is not set up correctly on the server. Ask an administrator to check the "
    "e-mail settings."
)
SERVICE_UNREACHABLE = (
    "An online service this feature depends on did not respond. It can be tried again later."
)
SERVICE_REJECTED = (
    "An online service rejected the request, which usually means a key or access problem. "
    "Ask an administrator to check the service settings."
)
SERVICE_BUSY = (
    "An online service is limiting requests right now. It can be tried again shortly."
)
DATABASE = "The database was busy or unavailable. It can be tried again."

_TECHNICAL = re.compile(
    r"\bb['\"]|\(\d{3}, |Traceback|\[Errno|errno\b|0x[0-9a-fA-F]{6,}|ConnectionPool|urllib3"
    r"|<[A-Za-z_.]+ object|sqlite3\.|\(sqlalche\.me|\bSQL:",
)
_NETWORK_WORDS = re.compile(
    r"\[Errno|getaddrinfo|timed out|refused|unreachable|reset by peer|connection (?:closed|aborted)"
    r"|name or service not known|network is",
    re.IGNORECASE,
)
_PROGRAMMING_ERRORS = (
    KeyError, IndexError, AttributeError, TypeError, NameError, ZeroDivisionError,
    AssertionError, NotImplementedError, UnicodeError, RecursionError,
)
_CLASS_PREFIX = re.compile(r"^(?:Unexpected\s+)?[A-Z][A-Za-z0-9_.]*(?:Error|Exception|Refused|Disconnected)\b:?\s*")
_RECIPIENTS_PREFIX = re.compile(r"^(\d+ of \d+ recipient\(s\) not delivered\.\s+)(.*)$", re.DOTALL)


def is_sign_in_failure(error: BaseException) -> bool:
    """The mail login was refused, or the server hung up while it was being checked.

    Neither can succeed on a retry, and each retry is one more failed login that makes
    the provider block the account for longer.
    """
    return isinstance(error, smtplib.SMTPAuthenticationError) or (
        isinstance(error, smtplib.SMTPServerDisconnected)
        and getattr(error, "smtp_operation", None) == "login"
    )


def _for_smtp_code(code: int) -> str:
    if 400 <= code < 500:
        return BUSY
    if code in (530, 534, 535):
        return SIGN_IN_REJECTED
    if code == 552:
        return TOO_LARGE
    if code in (550, 551, 553):
        return RECIPIENT_REFUSED
    return REJECTED


def _readable(message: str) -> str:
    message = _CLASS_PREFIX.sub("", message).strip()
    return GENERIC if not message or _TECHNICAL.search(message) else message


def _from_exception(error: BaseException, *, email: bool) -> str:
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return SIGN_IN_REJECTED
    if isinstance(error, smtplib.SMTPSenderRefused):
        return SENDER_REFUSED
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in error.recipients.values()]
        return RECIPIENT_LATER if codes and all(400 <= c < 500 for c in codes) else RECIPIENT_REFUSED
    if isinstance(error, smtplib.SMTPServerDisconnected) and is_sign_in_failure(error):
        return SIGN_IN_BLOCKED
    # Connection errors are also response errors, so they must be checked first.
    if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, smtplib.SMTPHeloError)):
        return UNREACHABLE
    if isinstance(error, smtplib.SMTPResponseException):
        return _for_smtp_code(error.smtp_code)
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return MAIL_SETUP
    if isinstance(error, smtplib.SMTPException):
        return REJECTED
    if isinstance(error, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror,
                          socket.herror, ssl.SSLError)):
        return UNREACHABLE
    if isinstance(error, _PROGRAMMING_ERRORS):
        return GENERIC  # their text ("'subject'", "list index out of range") means nothing to a person
    message = str(error).strip()
    if isinstance(error, OSError) and email and _NETWORK_WORDS.search(message):
        return UNREACHABLE
    if isinstance(error, ValueError) and email and re.search(r"smtp|e-?mail|sender|provider", message, re.I) \
            and re.search(r"configur|missing|invalid|not set|required", message, re.I):
        return MAIL_SETUP
    module = type(error).__module__.split(".")[0]
    name = type(error).__name__
    if module in {"httpx", "requests", "urllib3", "groq", "openai", "httpcore"}:
        if re.search(r"Authentication|PermissionDenied|Unauthorized|Forbidden", name):
            return SERVICE_REJECTED
        if "RateLimit" in name:
            return SERVICE_BUSY
        return SERVICE_UNREACHABLE
    if module == "sqlalchemy" and re.search(r"Operational|DBAPI|Timeout|Interface", name):
        return DATABASE
    return _readable(message or name)


def _from_text(text: str) -> str:
    prefix = _RECIPIENTS_PREFIX.match(text)
    if prefix:
        return prefix.group(1) + _from_text(prefix.group(2))
    if re.search(r"SMTPAuthenticationError|Username and Password not accepted|BadCredentials|\(535,", text):
        return SIGN_IN_REJECTED
    if re.match(r"(?:SMTPRecipientsRefused|Recipient refused)", text):
        return RECIPIENT_REFUSED
    code = re.match(r"Mail server replied (\d{3})\b", text)
    if code:
        return _for_smtp_code(int(code.group(1)))
    if re.match(r"(?:SMTPServerDisconnected|SMTPConnectError|SMTPHeloError|ConnectionError|"
                r"ConnectionRefusedError|ConnectionResetError|TimeoutError|SSLError|gaierror|herror|"
                r"socket\.\w+)\b", text):
        return UNREACHABLE
    if re.match(r"(?:Unexpected\s+)?OSError\b", text):
        rest = _CLASS_PREFIX.sub("", text).strip()
        return UNREACHABLE if _NETWORK_WORDS.search(rest) else _readable(rest)
    if _CLASS_PREFIX.match(text) or _TECHNICAL.search(text):
        return _readable(text)
    return text


def plain_error(error: BaseException | str | None, *, email: bool = False) -> str | None:
    """A sentence a person can act on for ``error``; ``None`` for no error.

    ``email=True`` marks a failure that happened while sending mail, where a bare
    ``OSError`` with network wording means "the server could not be reached".
    """
    if error is None:
        return None
    if isinstance(error, BaseException):
        return _from_exception(error, email=email)
    text = str(error).strip()
    return _from_text(text) if text else None


def _clean(value: object) -> object:
    return plain_error(value) if isinstance(value, str) else value


# For API response models: rows saved before this module existed are reworded as they
# are sent, so nobody sees the old technical text.
PlainError = Annotated[str | None, BeforeValidator(_clean)]
