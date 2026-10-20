"""Local password authentication and short-lived, revocable access tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import uuid

import jwt
from pwdlib import PasswordHash
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.meeting.models import AuthAccount, AuthSession, Employee

logger = logging.getLogger(__name__)
PASSWORD_HASHER = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("not-a-real-account-password")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_JWT_ALGORITHM = "HS256"


class AuthConfigurationError(RuntimeError):
    """The server is missing a required authentication setting."""


class InvalidAccessToken(Exception):
    """The access token, account, employee, or session is invalid."""


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user_id: str
    account_id: str
    email: str
    name: str
    role: str
    team: str | None
    jti: str


@dataclass(frozen=True)
class TokenResult:
    token: str
    expires_in: int
    principal: AuthenticatedPrincipal


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def _jwt_settings() -> tuple[bytes, str, str, int]:
    secret = os.getenv("AUTH_JWT_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)
        secret = os.getenv("AUTH_JWT_SECRET", "")
    secret_bytes = secret.encode("utf-8")
    if len(secret_bytes) < 32:
        raise AuthConfigurationError(
            "AUTH_JWT_SECRET must contain at least 32 bytes of high-entropy secret material."
        )
    issuer = os.getenv("AUTH_JWT_ISSUER", "ai-office-operations").strip()
    audience = os.getenv("AUTH_JWT_AUDIENCE", "ai-office-web").strip()
    try:
        expires_in = int(os.getenv("AUTH_ACCESS_TOKEN_TTL_SECONDS", "900"))
    except ValueError as exc:
        raise AuthConfigurationError("AUTH_ACCESS_TOKEN_TTL_SECONDS must be an integer.") from exc
    if not issuer or not audience or not 60 <= expires_in <= 86400:
        raise AuthConfigurationError("JWT issuer, audience, or token lifetime configuration is invalid.")
    return secret_bytes, issuer, audience, expires_in


def provision_account(
    db: Session,
    employee_email: str,
    password: str,
    role: str = "employee",
) -> AuthAccount:
    """Provision a login only for one existing active employee; never creates employees."""
    email = normalize_email(employee_email)
    if not _EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Provide a valid employee email address.")
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Passwords must be between 12 and 1024 characters.")
    if role not in ("employee", "manager", "hr_operations", "admin"):
        raise ValueError("Unsupported account role.")

    employees = (
        db.query(Employee)
        .filter(func.lower(func.trim(Employee.email)) == email)
        .all()
    )
    if len(employees) != 1:
        raise ValueError("The email must match exactly one existing employee.")
    employee = employees[0]
    if not employee.is_active:
        raise ValueError("Cannot provision a disabled employee.")
    existing = (
        db.query(AuthAccount)
        .filter(
            (AuthAccount.employee_id == employee.id)
            | (AuthAccount.email_normalized == email)
        )
        .first()
    )
    if existing:
        raise ValueError("An authentication account already exists for this employee or email.")

    account = AuthAccount(
        id=str(uuid.uuid4()),
        employee_id=employee.id,
        email_normalized=email,
        password_hash=PASSWORD_HASHER.hash(password),
        role=role,
        is_enabled=True,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def authenticate_password(
    db: Session,
    email: str,
    password: str,
) -> tuple[AuthAccount, Employee] | None:
    normalized_email = normalize_email(email)
    account = (
        db.query(AuthAccount)
        .filter(AuthAccount.email_normalized == normalized_email)
        .first()
    )
    stored_hash = account.password_hash if account else _DUMMY_PASSWORD_HASH
    try:
        valid_password = PASSWORD_HASHER.verify(password, stored_hash)
    except Exception:
        logger.warning("Password hash verification failed.")
        valid_password = False
    if not account or not account.is_enabled or not valid_password:
        return None

    employee = db.query(Employee).filter(Employee.id == account.employee_id).first()
    if employee is None or not employee.is_active:
        return None
    if normalize_email(employee.email) != account.email_normalized:
        # A changed or mismapped directory address requires operator repair.
        return None
    return account, employee


def _principal(account: AuthAccount, employee: Employee, jti: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=employee.id,
        account_id=account.id,
        email=employee.email,
        name=employee.name,
        role=account.role,
        team=employee.team,
        jti=jti,
    )


def issue_access_token(
    db: Session,
    account: AuthAccount,
    employee: Employee,
) -> TokenResult:
    secret, issuer, audience, expires_in = _jwt_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)
    jti = str(uuid.uuid4())
    token = jwt.encode(
        {
            "sub": account.id,
            "iss": issuer,
            "aud": audience,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": jti,
        },
        secret,
        algorithm=_JWT_ALGORITHM,
    )
    session = AuthSession(
        jti=jti,
        account_id=account.id,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    return TokenResult(token=token, expires_in=expires_in, principal=_principal(account, employee, jti))


def authenticate_access_token(db: Session, token: str) -> AuthenticatedPrincipal:
    secret, issuer, audience, _ = _jwt_settings()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_JWT_ALGORITHM],
            issuer=issuer,
            audience=audience,
            options={"require": ["sub", "iss", "aud", "iat", "exp", "jti"]},
        )
        account_id = claims["sub"]
        jti = claims["jti"]
        if not isinstance(account_id, str) or not isinstance(jti, str):
            raise InvalidAccessToken()
        uuid.UUID(account_id)
        uuid.UUID(jti)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise InvalidAccessToken() from exc

    session = db.query(AuthSession).filter(AuthSession.jti == jti).first()
    if session is None or session.account_id != account_id or session.revoked_at is not None:
        raise InvalidAccessToken()
    session_expiry = session.expires_at
    if session_expiry.tzinfo is None:
        session_expiry = session_expiry.replace(tzinfo=timezone.utc)
    if session_expiry <= datetime.now(timezone.utc):
        raise InvalidAccessToken()

    account = db.query(AuthAccount).filter(AuthAccount.id == account_id).first()
    if account is None or not account.is_enabled:
        raise InvalidAccessToken()
    employee = db.query(Employee).filter(Employee.id == account.employee_id).first()
    if employee is None or not employee.is_active:
        raise InvalidAccessToken()
    if normalize_email(employee.email) != account.email_normalized:
        raise InvalidAccessToken()
    return _principal(account, employee, jti)


def revoke_access_token(db: Session, principal: AuthenticatedPrincipal) -> None:
    session = db.query(AuthSession).filter(AuthSession.jti == principal.jti).first()
    if session is not None and session.account_id == principal.account_id and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()