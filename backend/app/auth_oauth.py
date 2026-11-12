"""Sign in with Google or Microsoft Entra ID using OAuth 2.0 / OpenID Connect (Story 10.1).

The flow is the authorization-code flow with PKCE:

1. ``start_authorization`` stores a one-time ``state``, ``nonce`` and PKCE verifier and returns
   the provider's sign-in URL.
2. The provider sends the browser back to our callback. ``complete_callback`` checks the
   state, swaps the code for an ID token, verifies the token (signature, issuer, audience,
   expiry, nonce) and links it to an *existing* employee account. Nobody is ever created
   by signing in.
3. The browser is sent to the front end with a one-time code, and ``exchange_login_code``
   swaps that for our own access and refresh tokens. Tokens are never put in a URL.

Provider credentials come only from environment variables. Nothing here logs or returns a
secret, a code, a token or a verifier.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy.orm import Session

from app.auth_service import normalize_email
from app.auth_sessions import (
    PROVIDER_LABELS,
    SessionTokens,
    check_session,
    create_login_session,
    hash_token,
    issue_session_tokens,
    record_audit,
    SessionEnded,
)
from app.meeting.models import (
    AuthAccount,
    AuthIdentity,
    AuthLoginSession,
    Employee,
    OAuthLoginState,
)

logger = logging.getLogger(__name__)

STATE_LIFETIME = timedelta(minutes=10)
EXCHANGE_CODE_LIFETIME = timedelta(minutes=2)
HTTP_TIMEOUT_SECONDS = 10
JWKS_CACHE_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60

# What the person sees. Deliberately vague about *why* an account was refused, so the
# sign-in page cannot be used to find out who has an account.
ERROR_MESSAGES = {
    "provider_error": "The sign-in was cancelled or the provider reported a problem. Please try again.",
    "invalid_state": "That sign-in link has expired or was already used. Please start again.",
    "provider_unavailable": "The sign-in provider could not be reached. Please try again in a moment.",
    "invalid_token": "The sign-in could not be verified. Please try again.",
    "email_unverified": "Your provider has not verified your e-mail address, so you cannot sign in with it.",
    "not_authorized": "This account is not set up to use AI Office. Ask an administrator for access.",
    "not_configured": "Sign-in with this provider is not available.",
    "invalid_code": "That sign-in has expired or was already completed. Please start again.",
}


class OAuthLoginError(Exception):
    """A sign-in that must not proceed. ``code`` is a key of ``ERROR_MESSAGES``."""

    def __init__(self, code: str, *, audit: str | None = None, user_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code if code in ERROR_MESSAGES else "invalid_token"
        self.audit = audit  # what to record in the audit trail (never shown to the person)
        self.user_id = user_id

    @property
    def message(self) -> str:
        return ERROR_MESSAGES[self.code]


@dataclass(frozen=True)
class ProviderConfig:
    key: str
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    jwks_url: str
    tenant: str | None = None  # Microsoft only
    allowed_domain: str | None = None  # Google only: restrict to one Workspace domain


@dataclass(frozen=True)
class ProviderIdentity:
    subject: str
    email: str
    name: str | None


# ---------------------------------------------------------------------------
# Configuration (environment variables only)
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def provider_config(provider: str) -> ProviderConfig | None:
    """The provider's settings, or None when it is not (fully) configured."""
    if provider == "google":
        client_id, secret = _env("GOOGLE_OAUTH_CLIENT_ID"), _env("GOOGLE_OAUTH_CLIENT_SECRET")
        if not (client_id and secret):
            return None
        return ProviderConfig(
            key="google",
            name="Google",
            client_id=client_id,
            client_secret=secret,
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            jwks_url="https://www.googleapis.com/oauth2/v3/certs",
            allowed_domain=_env("GOOGLE_OAUTH_ALLOWED_DOMAIN").lower() or None,
        )
    if provider == "microsoft":
        client_id, secret = _env("MICROSOFT_OAUTH_CLIENT_ID"), _env("MICROSOFT_OAUTH_CLIENT_SECRET")
        tenant = _env("MICROSOFT_OAUTH_TENANT_ID")
        # A specific tenant is required. "common" / "organizations" would accept any
        # Microsoft tenant, and Entra does not verify the e-mail address a tenant puts in
        # its tokens, so matching on e-mail would let another tenant impersonate a person.
        if not (client_id and secret and tenant) or tenant.lower() in {"common", "organizations", "consumers"}:
            return None
        base = f"https://login.microsoftonline.com/{tenant}"
        return ProviderConfig(
            key="microsoft",
            name="Microsoft",
            client_id=client_id,
            client_secret=secret,
            authorize_url=f"{base}/oauth2/v2.0/authorize",
            token_url=f"{base}/oauth2/v2.0/token",
            jwks_url=f"{base}/discovery/v2.0/keys",
            tenant=tenant,
        )
    return None


def available_providers() -> list[dict]:
    return [
        {"id": key, "name": name, "enabled": provider_config(key) is not None}
        for key, name in (("google", "Google"), ("microsoft", "Microsoft"))
    ]


def redirect_uri(provider: str) -> str:
    base = (_env("OAUTH_REDIRECT_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/api/v1/auth/oauth/{provider}/callback"


def frontend_base_url() -> str:
    return (_env("FRONTEND_BASE_URL") or "http://localhost:5173").rstrip("/")


# ---------------------------------------------------------------------------
# Network calls (separate functions so tests can stand in for the providers)
# ---------------------------------------------------------------------------


def _post_token_request(url: str, data: dict) -> dict:
    response = httpx.post(url, data=data, timeout=HTTP_TIMEOUT_SECONDS, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise OAuthLoginError("provider_unavailable", audit=f"Token request returned HTTP {response.status_code}.")
    return response.json()


def _fetch_jwks(url: str) -> dict:
    response = httpx.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


_jwks_cache: dict[str, tuple[float, dict]] = {}


def _signing_key(config: ProviderConfig, kid: str):
    """The provider's public key for ``kid``; keys are cached, and refetched once on a miss."""
    for attempt in (0, 1):
        cached = _jwks_cache.get(config.jwks_url)
        if cached is None or attempt == 1 or time.time() - cached[0] > JWKS_CACHE_SECONDS:
            try:
                _jwks_cache[config.jwks_url] = (time.time(), _fetch_jwks(config.jwks_url))
            except Exception as error:  # noqa: BLE001 - network or provider failure
                raise OAuthLoginError("provider_unavailable", audit=f"Could not fetch signing keys: {type(error).__name__}.") from error
        for jwk in _jwks_cache[config.jwks_url][1].get("keys", []):
            if jwk.get("kid") == kid:
                return jwt.PyJWK.from_dict(jwk).key
    raise OAuthLoginError("invalid_token", audit="The ID token was signed with an unknown key.")


# ---------------------------------------------------------------------------
# Step 1: start
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def start_authorization(db: Session, provider: str) -> str:
    config = provider_config(provider)
    if config is None:
        raise OAuthLoginError("not_configured")

    # Housekeeping: finished or abandoned sign-ins are not kept.
    db.query(OAuthLoginState).filter(
        OAuthLoginState.created_at < _now() - timedelta(days=1)
    ).delete(synchronize_session=False)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    db.add(
        OAuthLoginState(
            id=str(uuid.uuid4()),
            state_hash=hash_token(state),
            provider=provider,
            nonce=nonce,
            code_verifier=verifier,
            created_at=_now(),
            expires_at=_now() + STATE_LIFETIME,
        )
    )
    db.commit()
    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{config.authorize_url}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Step 2: the callback
# ---------------------------------------------------------------------------


def _verify_id_token(config: ProviderConfig, id_token: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as error:
        raise OAuthLoginError("invalid_token", audit="The ID token could not be read.") from error
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise OAuthLoginError("invalid_token", audit="The ID token used an unexpected algorithm.")
    key = _signing_key(config, header["kid"])
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=config.client_id,
            leeway=CLOCK_SKEW_SECONDS,
            # The issuer depends on the provider (and, for Microsoft, the tenant): checked below.
            options={"require": ["exp", "iat", "sub", "aud", "iss"], "verify_iss": False},
        )
    except jwt.ExpiredSignatureError as error:
        raise OAuthLoginError("invalid_token", audit="The ID token had expired.") from error
    except jwt.PyJWTError as error:
        raise OAuthLoginError("invalid_token", audit=f"The ID token was rejected ({type(error).__name__}).") from error

    issuer = claims.get("iss")
    if config.key == "google":
        valid_issuer = issuer in ("https://accounts.google.com", "accounts.google.com")
    else:
        tenant_id = claims.get("tid")
        valid_issuer = bool(tenant_id) and issuer == f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        if valid_issuer and len(config.tenant or "") == 36 and tenant_id.lower() != config.tenant.lower():
            valid_issuer = False  # a different tenant's token
    if not valid_issuer:
        raise OAuthLoginError("invalid_token", audit="The ID token came from an unexpected issuer.")

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise OAuthLoginError("invalid_token", audit="The ID token's nonce did not match.")
    return claims


def _identity_from_claims(config: ProviderConfig, claims: dict) -> ProviderIdentity:
    if config.key == "google":
        email = claims.get("email")
        if claims.get("email_verified") is not True:
            raise OAuthLoginError("email_unverified", audit="Google reported the e-mail as unverified.")
        if config.allowed_domain and str(claims.get("hd", "")).lower() != config.allowed_domain:
            raise OAuthLoginError("not_authorized", audit="The Google account is outside the allowed domain.")
        subject = str(claims["sub"])
    else:
        email = claims.get("email") or claims.get("preferred_username")
        # `oid` is the person's stable id in the tenant; `sub` is specific to this app.
        subject = f"{claims.get('tid')}:{claims.get('oid') or claims['sub']}"
    if not isinstance(email, str) or "@" not in email:
        raise OAuthLoginError("invalid_token", audit="The ID token carried no e-mail address.")
    name = claims.get("name")
    return ProviderIdentity(subject=subject[:255], email=normalize_email(email), name=name if isinstance(name, str) else None)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _resolve_account(db: Session, provider: str, identity: ProviderIdentity) -> tuple[AuthAccount, Employee]:
    """The existing account this identity belongs to, linking it on first use."""
    known = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.provider == provider, AuthIdentity.subject == identity.subject)
        .first()
    )
    if known is not None:
        account = db.get(AuthAccount, known.account_id)
    else:
        account = db.query(AuthAccount).filter(AuthAccount.email_normalized == identity.email).first()

    employee = db.get(Employee, account.employee_id) if account else None
    problem = None
    if account is None or employee is None:
        problem = f"No account matches {mask_email(identity.email)}."
    elif not account.is_enabled or not employee.is_active:
        problem = "The account or employee is disabled."
    elif normalize_email(employee.email) != account.email_normalized:
        problem = "The account's e-mail no longer matches the employee directory."
    if problem:
        raise OAuthLoginError("not_authorized", audit=problem, user_id=employee.id if employee else None)

    if known is None:
        other = (
            db.query(AuthIdentity)
            .filter(AuthIdentity.account_id == account.id, AuthIdentity.provider == provider)
            .first()
        )
        if other is not None:
            # The account is already tied to a different person at this provider.
            raise OAuthLoginError(
                "not_authorized",
                audit=f"{mask_email(identity.email)} is already linked to a different {PROVIDER_LABELS[provider]} identity.",
                user_id=employee.id,
            )
        known = AuthIdentity(
            id=str(uuid.uuid4()),
            account_id=account.id,
            provider=provider,
            subject=identity.subject,
            email=identity.email,
            created_at=_now(),
        )
        db.add(known)
    known.last_login_at = _now()
    known.email = identity.email
    return account, employee


def _fail(db: Session, provider: str, error: OAuthLoginError, ip: str | None, user_agent: str | None) -> None:
    label = PROVIDER_LABELS.get(provider, provider)
    record_audit(
        db,
        "login_failed",
        user_id=error.user_id,
        provider=provider,
        ip_address=ip,
        user_agent=user_agent,
        detail=f"{label} sign-in failed: {error.audit or ERROR_MESSAGES[error.code]}",
    )


def complete_callback(
    db: Session,
    provider: str,
    *,
    code: str | None,
    state: str | None,
    provider_error: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Finish the provider redirect. Returns the one-time code for the front end, or raises."""
    config = provider_config(provider)
    row = db.query(OAuthLoginState).filter(OAuthLoginState.state_hash == hash_token(state or "")).first() if state else None
    try:
        if config is None:
            raise OAuthLoginError("not_configured")
        # The state is single-use: spent before anything else happens, valid or not.
        if row is None or row.provider != provider or row.consumed_at is not None or _aware(row.expires_at) <= _now():
            raise OAuthLoginError("invalid_state", audit="The sign-in state was unknown, expired or already used.")
        row.consumed_at = _now()
        db.commit()
        if provider_error or not code:
            raise OAuthLoginError("provider_error", audit=f"The provider returned an error ({(provider_error or 'no code')[:60]}).")

        try:
            token_response = _post_token_request(
                config.token_url,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri(provider),
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "code_verifier": row.code_verifier,
                },
            )
        except OAuthLoginError:
            raise
        except Exception as error:  # noqa: BLE001 - network failure
            raise OAuthLoginError("provider_unavailable", audit=f"The token request failed ({type(error).__name__}).") from error
        id_token = token_response.get("id_token") if isinstance(token_response, dict) else None
        if not isinstance(id_token, str):
            raise OAuthLoginError("invalid_token", audit="The provider returned no ID token.")

        claims = _verify_id_token(config, id_token, row.nonce)
        identity = _identity_from_claims(config, claims)
        account, employee = _resolve_account(db, provider, identity)
        session = create_login_session(
            db, account, employee, provider, ip_address=ip_address, user_agent=user_agent
        )
    except OAuthLoginError as error:
        db.rollback()
        if error.code != "not_configured":
            _fail(db, provider, error, ip_address, user_agent)
        raise

    exchange = secrets.token_urlsafe(32)
    row.exchange_hash = hash_token(exchange)
    row.exchange_expires_at = _now() + EXCHANGE_CODE_LIFETIME
    row.session_id = session.id
    db.commit()
    return exchange


# ---------------------------------------------------------------------------
# Step 3: swap the one-time code for our tokens
# ---------------------------------------------------------------------------


def exchange_login_code(db: Session, exchange_code: str) -> SessionTokens:
    row = (
        db.query(OAuthLoginState)
        .filter(OAuthLoginState.exchange_hash == hash_token(exchange_code or ""))
        .first()
    )
    if row is None or row.session_id is None or _aware(row.exchange_expires_at) <= _now():
        raise OAuthLoginError("invalid_code")
    # Single use, even if two requests arrive at once.
    spent = (
        db.query(OAuthLoginState)
        .filter(OAuthLoginState.id == row.id, OAuthLoginState.exchanged_at.is_(None))
        .update({"exchanged_at": _now()}, synchronize_session=False)
    )
    db.commit()
    if spent == 0:
        raise OAuthLoginError("invalid_code")

    session = db.get(AuthLoginSession, row.session_id)
    try:
        check_session(db, session, touch=False)
    except SessionEnded as error:
        raise OAuthLoginError("invalid_code") from error
    account = db.get(AuthAccount, session.account_id)
    employee = db.get(Employee, account.employee_id)
    return issue_session_tokens(db, session, account, employee)
