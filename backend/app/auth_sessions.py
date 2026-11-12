"""Login sessions, rotating refresh tokens, idle timeout and revocation (Story 10.1).

A *login session* is one sign-in. Short-lived access tokens (15 minutes) point at it, and a
refresh token renews them. Three things end a session before its absolute expiry:

* **Idle timeout** - no authenticated activity for ``AUTH_IDLE_TIMEOUT_SECONDS``.
* **Revocation** - the person signs out, or an administrator ends the session.
* **Refresh-token reuse** - a refresh token is single-use. Presenting one that was already
  exchanged means it was copied, so the whole session is ended, new token included.

Refresh tokens are random, only their SHA-256 hash is stored, and nothing here logs a token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
import re
import secrets
import uuid

from sqlalchemy.orm import Session

from app.auth_service import TokenResult, issue_access_token, normalize_email
from app.meeting.models import (
    AuthAccount,
    AuthAuditEvent,
    AuthLoginSession,
    AuthRefreshToken,
    AuthSession,
    Employee,
)

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {"password": "password", "google": "Google", "microsoft": "Microsoft"}

# Why a session ended -> what the person is told.
REASON_MESSAGES = {
    "admin": "Your session was ended by an administrator. Please sign in again.",
    "idle_timeout": "You were signed out because you were inactive. Please sign in again.",
    "expired": "Your session has expired. Please sign in again.",
    "logout": "You have signed out. Please sign in again.",
    "refresh_reuse": "Your session was ended for security reasons. Please sign in again.",
    "account_disabled": "Your account is no longer active. Contact an administrator.",
    "invalid": "Your session is no longer valid. Please sign in again.",
}


class SessionEnded(Exception):
    """The session cannot be used any more. ``reason`` is a key of ``REASON_MESSAGES``."""

    def __init__(self, reason: str = "invalid") -> None:
        super().__init__(reason)
        self.reason = reason if reason in REASON_MESSAGES else "invalid"

    @property
    def message(self) -> str:
        return REASON_MESSAGES[self.reason]


@dataclass(frozen=True)
class SessionTokens:
    access: TokenResult
    refresh_token: str
    refresh_expires_in: int
    session_id: str


# ---------------------------------------------------------------------------
# Settings and small helpers
# ---------------------------------------------------------------------------


def _int_setting(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def idle_timeout_seconds() -> int:
    return _int_setting("AUTH_IDLE_TIMEOUT_SECONDS", 1800, 60)


def refresh_token_ttl_seconds() -> int:
    return _int_setting("AUTH_REFRESH_TOKEN_TTL_SECONDS", 86400, 60)


def session_max_age_seconds() -> int:
    return _int_setting("AUTH_SESSION_MAX_AGE_SECONDS", 604800, 300)


# Recording activity on every request would be a database write per request.
ACTIVITY_WRITE_INTERVAL = timedelta(seconds=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clip(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    return value.strip()[:limit] or None


def describe_device(user_agent: str | None) -> str:
    """'Chrome on Windows' from a User-Agent string; never anything more identifying."""
    text = user_agent or ""
    browser = next(
        (name for pattern, name in (
            (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/", "Firefox"),
            (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari"),
        ) if re.search(pattern, text)),
        None,
    )
    system = next(
        (name for pattern, name in (
            (r"Windows", "Windows"), (r"Android", "Android"), (r"iPhone|iPad|iOS", "iOS"),
            (r"Mac OS X|Macintosh", "macOS"), (r"Linux", "Linux"),
        ) if re.search(pattern, text)),
        None,
    )
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"


def record_audit(
    db: Session,
    event: str,
    *,
    user_id: str | None = None,
    actor_id: str | None = None,
    provider: str | None = None,
    session_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: str = "",
    commit: bool = True,
) -> AuthAuditEvent:
    """Append one row to the auth audit trail. Never given a token, code or password."""
    row = AuthAuditEvent(
        id=str(uuid.uuid4()),
        event=event,
        user_id=user_id,
        actor_id=actor_id,
        provider=provider,
        session_id=session_id,
        ip_address=_clip(ip_address, 64),
        user_agent=_clip(user_agent, 300),
        detail=detail,
        created_at=_now(),
    )
    db.add(row)
    if commit:
        db.commit()
    return row


# ---------------------------------------------------------------------------
# Creating sessions and tokens
# ---------------------------------------------------------------------------


def create_login_session(
    db: Session,
    account: AuthAccount,
    employee: Employee,
    provider: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuthLoginSession:
    """Open a session and record the successful login in the audit trail."""
    now = _now()
    session = AuthLoginSession(
        id=str(uuid.uuid4()),
        account_id=account.id,
        provider=provider,
        ip_address=_clip(ip_address, 64),
        user_agent=_clip(user_agent, 300),
        created_at=now,
        last_active_at=now,
        expires_at=now + timedelta(seconds=session_max_age_seconds()),
    )
    db.add(session)
    place = f" from {session.ip_address}" if session.ip_address else ""
    record_audit(
        db,
        "login_success",
        user_id=employee.id,
        provider=provider,
        session_id=session.id,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=f"Signed in with {PROVIDER_LABELS.get(provider, provider)}{place} "
        f"({describe_device(user_agent)}).",
        commit=False,
    )
    db.commit()
    return session


def _new_refresh_token(db: Session, session: AuthLoginSession) -> tuple[str, int]:
    now = _now()
    session_end = _aware(session.expires_at)
    expires_at = min(now + timedelta(seconds=refresh_token_ttl_seconds()), session_end)
    raw = secrets.token_urlsafe(48)
    db.add(
        AuthRefreshToken(
            id=str(uuid.uuid4()),
            session_id=session.id,
            token_hash=hash_token(raw),
            created_at=now,
            expires_at=expires_at,
        )
    )
    return raw, max(1, int((expires_at - now).total_seconds()))


def issue_session_tokens(
    db: Session, session: AuthLoginSession, account: AuthAccount, employee: Employee
) -> SessionTokens:
    """An access token and a fresh refresh token for an open session."""
    access = issue_access_token(db, account, employee, login_session_id=session.id)
    refresh, refresh_expires_in = _new_refresh_token(db, session)
    db.commit()
    return SessionTokens(access, refresh, refresh_expires_in, session.id)


def start_session(
    db: Session,
    account: AuthAccount,
    employee: Employee,
    provider: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionTokens:
    session = create_login_session(
        db, account, employee, provider, ip_address=ip_address, user_agent=user_agent
    )
    return issue_session_tokens(db, session, account, employee)


# ---------------------------------------------------------------------------
# Using, renewing and ending sessions
# ---------------------------------------------------------------------------


def _end_session(db: Session, session: AuthLoginSession, *, reason: str, by: str | None) -> None:
    now = _now()
    if session.revoked_at is None:
        session.revoked_at = now
        session.revoked_by = by
        session.revoked_reason = reason
    # The access tokens issued under it stop working at once.
    db.query(AuthSession).filter(
        AuthSession.login_session_id == session.id, AuthSession.revoked_at.is_(None)
    ).update({"revoked_at": now}, synchronize_session=False)
    db.commit()


def check_session(db: Session, session: AuthLoginSession | None, *, touch: bool = True) -> None:
    """Raise ``SessionEnded`` unless the session may still be used; record activity if it may."""
    if session is None:
        raise SessionEnded("invalid")
    now = _now()
    if session.revoked_at is not None:
        raise SessionEnded(session.revoked_reason or "invalid")
    if _aware(session.expires_at) <= now:
        raise SessionEnded("expired")
    last_active = _aware(session.last_active_at)
    if last_active + timedelta(seconds=idle_timeout_seconds()) <= now:
        _end_session(db, session, reason="idle_timeout", by=None)
        account = db.get(AuthAccount, session.account_id)
        record_audit(
            db,
            "session_idle_timeout",
            user_id=account.employee_id if account else None,
            provider=session.provider,
            session_id=session.id,
            ip_address=session.ip_address,
            detail=f"Signed out after {idle_timeout_seconds() // 60} minutes of inactivity.",
        )
        raise SessionEnded("idle_timeout")
    if touch and now - last_active >= ACTIVITY_WRITE_INTERVAL:
        session.last_active_at = now
        db.commit()


def rotate_refresh_token(
    db: Session,
    raw_token: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionTokens:
    """Exchange a refresh token for a new access token and a new refresh token.

    The presented token is spent. If it was already spent, the token was copied: the whole
    session ends and the event is audited.
    """
    token = (
        db.query(AuthRefreshToken)
        .filter(AuthRefreshToken.token_hash == hash_token(raw_token or ""))
        .first()
    )
    if token is None:
        raise SessionEnded("invalid")
    session = db.get(AuthLoginSession, token.session_id)
    if session is None:
        raise SessionEnded("invalid")
    now = _now()

    # Spend the token atomically, so two simultaneous requests cannot both succeed.
    spent = (
        db.query(AuthRefreshToken)
        .filter(AuthRefreshToken.id == token.id, AuthRefreshToken.used_at.is_(None))
        .update({"used_at": now}, synchronize_session=False)
    )
    db.commit()
    account = db.get(AuthAccount, session.account_id)
    if spent == 0:
        _end_session(db, session, reason="refresh_reuse", by=None)
        record_audit(
            db,
            "refresh_reuse_detected",
            user_id=account.employee_id if account else None,
            provider=session.provider,
            session_id=session.id,
            ip_address=ip_address,
            user_agent=user_agent,
            detail="A refresh token that had already been used was presented again. "
            "The session was ended.",
        )
        raise SessionEnded("refresh_reuse")
    if _aware(token.expires_at) <= now:
        raise SessionEnded("expired")

    check_session(db, session, touch=False)
    employee = db.get(Employee, account.employee_id) if account else None
    if account is None or not account.is_enabled or employee is None or not employee.is_active \
            or normalize_email(employee.email) != account.email_normalized:
        _end_session(db, session, reason="account_disabled", by=None)
        raise SessionEnded("account_disabled")

    session.last_active_at = now
    return issue_session_tokens(db, session, account, employee)


def end_own_session(db: Session, session_id: str | None, *, user_id: str) -> None:
    """Sign-out: end the caller's own session."""
    session = db.get(AuthLoginSession, session_id) if session_id else None
    if session is None or session.revoked_at is not None:
        return
    _end_session(db, session, reason="logout", by=user_id)
    record_audit(
        db, "logout", user_id=user_id, provider=session.provider, session_id=session.id,
        ip_address=session.ip_address, detail="Signed out.",
    )


# ---------------------------------------------------------------------------
# Administrator view
# ---------------------------------------------------------------------------


def _active_filter(query, now: datetime):
    return query.filter(
        AuthLoginSession.revoked_at.is_(None),
        AuthLoginSession.expires_at > now,
        AuthLoginSession.last_active_at > now - timedelta(seconds=idle_timeout_seconds()),
    )


def list_active_sessions(db: Session, *, current_session_id: str | None = None) -> dict:
    now = _now()
    rows = (
        _active_filter(
            db.query(AuthLoginSession, AuthAccount, Employee)
            .join(AuthAccount, AuthAccount.id == AuthLoginSession.account_id)
            .join(Employee, Employee.id == AuthAccount.employee_id),
            now,
        )
        .order_by(AuthLoginSession.last_active_at.desc())
        .all()
    )
    return {
        "idle_timeout_seconds": idle_timeout_seconds(),
        "sessions": [
            {
                "id": session.id,
                "user": {
                    "id": employee.id,
                    "name": employee.name,
                    "email": employee.email,
                    "role": account.role,
                },
                "provider": session.provider,
                "provider_label": PROVIDER_LABELS.get(session.provider, session.provider),
                "signed_in_at": _aware(session.created_at).isoformat(),
                "last_active_at": _aware(session.last_active_at).isoformat(),
                "expires_at": _aware(session.expires_at).isoformat(),
                "ip_address": session.ip_address,
                "device": describe_device(session.user_agent),
                "is_current": session.id == current_session_id,
            }
            for session, account, employee in rows
        ],
    }


class SessionNotFound(Exception):
    pass


def revoke_session_as_admin(db: Session, session_id: str, *, admin_id: str) -> dict:
    """End one session. Returns who it belonged to (for the response and the audit entry)."""
    session = db.get(AuthLoginSession, session_id)
    if session is None:
        raise SessionNotFound()
    account = db.get(AuthAccount, session.account_id)
    user_id = account.employee_id if account else None
    if session.revoked_at is None:
        _end_session(db, session, reason="admin", by=admin_id)
        record_audit(
            db, "session_revoked", user_id=user_id, actor_id=admin_id, provider=session.provider,
            session_id=session.id, ip_address=session.ip_address,
            detail="An administrator ended this session.",
        )
    return {"id": session.id, "user_id": user_id, "revoked": True}


def revoke_user_sessions_as_admin(db: Session, employee_id: str, *, admin_id: str) -> int:
    """End every active session of one person. Returns how many were ended."""
    now = _now()
    sessions = (
        _active_filter(
            db.query(AuthLoginSession)
            .join(AuthAccount, AuthAccount.id == AuthLoginSession.account_id)
            .filter(AuthAccount.employee_id == employee_id),
            now,
        ).all()
    )
    for session in sessions:
        revoke_session_as_admin(db, session.id, admin_id=admin_id)
    return len(sessions)
