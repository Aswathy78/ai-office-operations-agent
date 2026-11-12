"""Story 10.1 - Google / Microsoft OAuth 2.0 / OIDC sign-in, refresh rotation and sessions.

The providers are replaced by a fake that signs real RS256 ID tokens, so the genuine
validation code (signature, issuer, audience, expiry, nonce, PKCE) is what runs.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import auth_oauth
from app.auth_service import provision_account
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AuthAccount,
    AuthAuditEvent,
    AuthIdentity,
    AuthLoginSession,
    AuthRefreshToken,
    AuthSession,
    Base,
    Employee,
    OAuthLoginState,
)

AUTH_SECRET = "test-only-secret-that-is-at-least-32-bytes-long"
PASSWORD = "correct horse battery staple"
GOOGLE_ID, GOOGLE_SECRET = "google-client-id.apps.test", "GOOGLE-CLIENT-SECRET-VALUE"
MS_ID, MS_SECRET = "ms-client-id-0000", "MICROSOFT-CLIENT-SECRET-VALUE"
TENANT = "11111111-2222-3333-4444-555555555555"
KID = "test-key"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key) -> dict:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    data.update(kid=KID, use="sig", alg="RS256")
    return data


class FakeProvider:
    """Stands in for Google's and Microsoft's token and key endpoints."""

    def __init__(self) -> None:
        self.token_requests: list[dict] = []
        self.response: dict | Exception = {}

    def post_token(self, url: str, data: dict) -> dict:
        self.token_requests.append({"url": url, **data})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def fetch_jwks(self, url: str) -> dict:
        return {"keys": [_jwk(PRIVATE_KEY)]}


@pytest.fixture()
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(auth_oauth, "_post_token_request", fake.post_token)
    monkeypatch.setattr(auth_oauth, "_fetch_jwks", fake.fetch_jwks)
    auth_oauth._jwks_cache.clear()
    yield fake
    auth_oauth._jwks_cache.clear()


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session, monkeypatch, provider):
    for name, value in (
        ("AUTH_JWT_SECRET", AUTH_SECRET),
        ("AUTH_JWT_ISSUER", "test-ai-office"),
        ("AUTH_JWT_AUDIENCE", "test-ai-office-client"),
        ("AUTH_ACCESS_TOKEN_TTL_SECONDS", "900"),
        ("AUTH_IDLE_TIMEOUT_SECONDS", "1800"),
        ("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_ID),
        ("GOOGLE_OAUTH_CLIENT_SECRET", GOOGLE_SECRET),
        ("MICROSOFT_OAUTH_CLIENT_ID", MS_ID),
        ("MICROSOFT_OAUTH_CLIENT_SECRET", MS_SECRET),
        ("MICROSOFT_OAUTH_TENANT_ID", TENANT),
        ("OAUTH_REDIRECT_BASE_URL", "http://127.0.0.1:8000"),
        ("FRONTEND_BASE_URL", "http://localhost:5173"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", raising=False)
    # These tests use the real Bearer-token check, not the suite's test-only principal shortcut.
    app.dependency_overrides.pop(require_authenticated_principal, None)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _person(db: Session, email: str, *, role: str = "employee", enabled: bool = True,
            active: bool = True, team: str = "Engineering") -> Employee:
    employee = Employee(id=f"emp-{email.split('@')[0]}", name=email.split("@")[0].title(),
                        email=email, team=team, is_active=True)
    db.add(employee)
    db.commit()
    provision_account(db, email, PASSWORD, role)
    if not enabled:
        db.query(AuthAccount).filter(AuthAccount.employee_id == employee.id).update({"is_enabled": False})
    if not active:
        employee.is_active = False
    db.commit()
    return employee


def _claims(provider_key: str, nonce: str, **over) -> dict:
    now = int(time.time())
    if provider_key == "google":
        claims = {"sub": "google-sub-1", "aud": GOOGLE_ID, "iss": "https://accounts.google.com",
                  "iat": now, "exp": now + 3600, "nonce": nonce,
                  "email": "ava@example.test", "email_verified": True, "name": "Ava"}
    else:
        claims = {"sub": "ms-app-sub", "oid": "ms-oid-1", "tid": TENANT, "aud": MS_ID,
                  "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
                  "iat": now, "exp": now + 3600, "nonce": nonce,
                  "email": "ava@example.test", "name": "Ava"}
    claims.update(over)
    return {key: value for key, value in claims.items() if value is not None}


def _start(client: TestClient, provider_key: str) -> dict:
    response = client.post(f"/api/v1/auth/oauth/{provider_key}/start")
    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()} | {"_url": url}


def _callback(client, provider_key, fake: FakeProvider, *, claims=None, key=PRIVATE_KEY, kid=KID,
              algorithm="RS256", state=None, code="provider-code", error=None, nonce=None, **overrides):
    """Run start + callback. Returns (redirect Location, start params)."""
    params = _start(client, provider_key)
    token_claims = claims if claims is not None else _claims(provider_key, nonce or params["nonce"], **overrides)
    fake.response = {"id_token": jwt.encode(token_claims, key, algorithm=algorithm, headers={"kid": kid})}
    query = {"state": state or params["state"]}
    if error:
        query["error"] = error
    else:
        query["code"] = code
    response = client.get(f"/api/v1/auth/oauth/{provider_key}/callback", params=query, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"], params


def _sign_in(client, provider_key, fake, **kwargs) -> dict:
    """A full sign-in; returns the login response body."""
    location, _ = _callback(client, provider_key, fake, **kwargs)
    assert "/auth/callback?code=" in location, location
    code = parse_qs(urlparse(location).query)["code"][0]
    response = client.post("/api/v1/auth/oauth/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(body_or_token) -> dict:
    token = body_or_token["access_token"] if isinstance(body_or_token, dict) else body_or_token
    return {"Authorization": f"Bearer {token}"}


def _error_of(location: str) -> str:
    return parse_qs(urlparse(location).query)["error"][0]


def _events(db: Session, event: str | None = None) -> list[AuthAuditEvent]:
    db.expire_all()
    query = db.query(AuthAuditEvent).order_by(AuthAuditEvent.created_at)
    return query.filter(AuthAuditEvent.event == event).all() if event else query.all()


def _password_login(client, email: str) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Providers and configuration
# ---------------------------------------------------------------------------


def test_providers_lists_what_is_configured_and_never_a_secret(client):
    body = client.get("/api/v1/auth/providers").json()
    assert {p["id"]: p["enabled"] for p in body["providers"]} == {"google": True, "microsoft": True}
    text = json.dumps(body)
    for secret in (GOOGLE_ID, GOOGLE_SECRET, MS_ID, MS_SECRET, TENANT):
        assert secret not in text


def test_a_provider_without_credentials_is_off_and_cannot_start(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET")
    body = client.get("/api/v1/auth/providers").json()
    assert {p["id"]: p["enabled"] for p in body["providers"]}["google"] is False
    assert client.post("/api/v1/auth/oauth/google/start").status_code == 503
    assert client.post("/api/v1/auth/oauth/github/start").status_code == 404


@pytest.mark.parametrize("tenant", ["common", "organizations", "consumers", ""])
def test_microsoft_needs_a_specific_tenant(client, monkeypatch, tenant):
    monkeypatch.setenv("MICROSOFT_OAUTH_TENANT_ID", tenant)
    providers = {p["id"]: p["enabled"] for p in client.get("/api/v1/auth/providers").json()["providers"]}
    assert providers["microsoft"] is False


def test_the_sign_in_url_has_state_nonce_and_pkce_but_no_secret(client):
    params = _start(client, "google")
    assert params["_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert params["client_id"] == GOOGLE_ID and params["response_type"] == "code"
    assert params["redirect_uri"] == "http://127.0.0.1:8000/api/v1/auth/oauth/google/callback"
    assert params["scope"] == "openid email profile"
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 32 and len(params["nonce"]) >= 24
    assert GOOGLE_SECRET not in params["_url"]
    ms = _start(client, "microsoft")
    assert ms["_url"].startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?")


# ---------------------------------------------------------------------------
# Signing in with each provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_key, label", [("google", "Google"), ("microsoft", "Microsoft")])
def test_employee_signs_in_with_each_provider(client, db_session, provider, provider_key, label):
    employee = _person(db_session, "ava@example.test", team="Engineering")

    body = _sign_in(client, provider_key, provider)

    assert body["user"]["id"] == employee.id and body["user"]["email"] == "ava@example.test"
    assert body["user"]["role"] == "employee"
    assert body["refresh_token"] and body["refresh_expires_in"] > 0
    me = client.get("/api/v1/auth/me", headers=_bearer(body))
    assert me.status_code == 200 and me.json()["id"] == employee.id

    # The access token lives for 15 minutes.
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert body["expires_in"] == 900 and claims["exp"] - claims["iat"] == 900

    # The identity is linked to the existing account, and nothing new was created.
    identity = db_session.query(AuthIdentity).one()
    assert identity.provider == provider_key and identity.account_id == f"{db_session.query(AuthAccount).one().id}"
    assert db_session.query(AuthAccount).count() == 1 and db_session.query(Employee).count() == 1


def test_microsoft_subject_includes_the_tenant_and_stable_object_id(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _sign_in(client, "microsoft", provider)
    assert db_session.query(AuthIdentity).one().subject == f"{TENANT}:ms-oid-1"


def test_microsoft_falls_back_to_the_user_principal_name(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "microsoft", provider, email=None, preferred_username="Ava@Example.test")
    assert body["user"]["email"] == "ava@example.test"


def test_the_token_request_carries_the_pkce_verifier_and_redirect_uri(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _, params = _callback(client, "google", provider)
    request = provider.token_requests[-1]
    assert request["url"] == "https://oauth2.googleapis.com/token"
    assert request["grant_type"] == "authorization_code" and request["code"] == "provider-code"
    assert request["redirect_uri"] == params["redirect_uri"]
    assert request["client_id"] == GOOGLE_ID and request["client_secret"] == GOOGLE_SECRET
    digest = hashlib.sha256(request["code_verifier"].encode("ascii")).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == params["code_challenge"]


def test_a_second_sign_in_reuses_the_linked_identity(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _sign_in(client, "google", provider)
    # The person's e-mail changed at Google, but the stable subject still matches.
    body = _sign_in(client, "google", provider, email="ava.new@example.test")
    assert body["user"]["email"] == "ava@example.test"
    assert db_session.query(AuthIdentity).count() == 1
    assert db_session.query(AuthLoginSession).count() == 2


def test_the_same_person_can_use_both_providers(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _sign_in(client, "google", provider)
    _sign_in(client, "microsoft", provider)
    assert {i.provider for i in db_session.query(AuthIdentity).all()} == {"google", "microsoft"}


# ---------------------------------------------------------------------------
# Refusals: unknown, disabled, conflicting and unverified identities
# ---------------------------------------------------------------------------


def test_an_unknown_person_is_refused_and_no_account_is_created(client, db_session, provider):
    _person(db_session, "someone.else@example.test")
    location, _ = _callback(client, "google", provider, email="stranger@example.test", sub="stranger-sub")
    assert _error_of(location) == "not_authorized"
    assert "auth/callback" not in location
    assert db_session.query(AuthAccount).count() == 1 and db_session.query(AuthIdentity).count() == 0
    failed = _events(db_session, "login_failed")[0]
    assert failed.provider == "google" and failed.ip_address == "testclient"
    assert "s***@example.test" in failed.detail and "stranger@" not in failed.detail


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"active": False}])
def test_a_disabled_account_or_employee_cannot_sign_in(client, db_session, provider, kwargs):
    _person(db_session, "ava@example.test", **kwargs)
    location, _ = _callback(client, "google", provider)
    assert _error_of(location) == "not_authorized"
    assert db_session.query(AuthLoginSession).count() == 0


def test_a_different_identity_cannot_take_over_a_linked_account(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _sign_in(client, "google", provider)   # links google-sub-1
    location, _ = _callback(client, "google", provider, sub="attacker-sub")   # same e-mail, new person
    assert _error_of(location) == "not_authorized"
    assert db_session.query(AuthIdentity).count() == 1
    assert "already linked" in _events(db_session, "login_failed")[0].detail


def test_google_must_have_verified_the_email(client, db_session, provider):
    _person(db_session, "ava@example.test")
    location, _ = _callback(client, "google", provider, email_verified=False)
    assert _error_of(location) == "email_unverified"
    location, _ = _callback(client, "google", provider, email_verified=None)
    assert _error_of(location) == "email_unverified"


def test_google_can_be_limited_to_one_workspace_domain(client, db_session, provider, monkeypatch):
    _person(db_session, "ava@example.test")
    monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", "example.test")
    assert _error_of(_callback(client, "google", provider)[0]) == "not_authorized"          # no hd claim
    assert "auth/callback" in _callback(client, "google", provider, hd="example.test")[0]
    assert _error_of(_callback(client, "google", provider, hd="evil.test")[0]) == "not_authorized"


def test_a_token_from_a_different_microsoft_tenant_is_refused(client, db_session, provider):
    _person(db_session, "ava@example.test")
    other = "99999999-9999-9999-9999-999999999999"
    location, _ = _callback(client, "microsoft", provider, tid=other,
                            iss=f"https://login.microsoftonline.com/{other}/v2.0")
    assert _error_of(location) == "invalid_token"


def test_the_refusal_message_does_not_reveal_whether_an_account_exists(client, db_session, provider):
    from app.auth_oauth import ERROR_MESSAGES

    _person(db_session, "ava@example.test", enabled=False)
    disabled = _error_of(_callback(client, "google", provider)[0])
    unknown = _error_of(_callback(client, "google", provider, email="nobody@example.test", sub="x")[0])
    assert disabled == unknown == "not_authorized"
    assert "disabled" not in ERROR_MESSAGES["not_authorized"].lower()


# ---------------------------------------------------------------------------
# ID-token and state validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("override", [
    {"aud": "someone-elses-client"},
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 3600},
    {"nonce": "not-the-nonce"},
])
def test_a_bad_id_token_is_rejected(client, db_session, provider, override):
    _person(db_session, "ava@example.test")
    location, _ = _callback(client, "google", provider, **override)
    assert _error_of(location) == "invalid_token"
    assert db_session.query(AuthLoginSession).count() == 0
    assert _events(db_session, "login_failed")


def test_an_id_token_without_a_nonce_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    claims = _claims("google", "irrelevant")
    del claims["nonce"]
    assert _error_of(_callback(client, "google", provider, claims=claims)[0]) == "invalid_token"


def test_a_token_signed_with_the_wrong_key_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    assert _error_of(_callback(client, "google", provider, key=OTHER_KEY)[0]) == "invalid_token"


def test_a_token_with_an_unknown_key_id_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    assert _error_of(_callback(client, "google", provider, kid="other-kid")[0]) == "invalid_token"


def test_an_unsigned_or_symmetric_token_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    params = _start(client, "google")
    for algorithm, key in (("none", None), ("HS256", GOOGLE_SECRET.ljust(32, "x"))):
        provider.response = {"id_token": jwt.encode(_claims("google", params["nonce"]), key, algorithm=algorithm,
                                                    headers={"kid": KID})}
        params = _start(client, "google")
        response = client.get("/api/v1/auth/oauth/google/callback",
                              params={"state": params["state"], "code": "x"}, follow_redirects=False)
        assert _error_of(response.headers["location"]) == "invalid_token"


def test_a_missing_or_unknown_state_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    assert _error_of(_callback(client, "google", provider, state="forged-state-value")[0]) == "invalid_state"
    response = client.get("/api/v1/auth/oauth/google/callback", params={"code": "x"}, follow_redirects=False)
    assert _error_of(response.headers["location"]) == "invalid_state"


def test_a_state_works_only_once(client, db_session, provider):
    _person(db_session, "ava@example.test")
    params = _start(client, "google")
    provider.response = {"id_token": jwt.encode(_claims("google", params["nonce"]), PRIVATE_KEY,
                                                algorithm="RS256", headers={"kid": KID})}
    query = {"state": params["state"], "code": "x"}
    first = client.get("/api/v1/auth/oauth/google/callback", params=query, follow_redirects=False)
    assert "auth/callback" in first.headers["location"]
    replay = client.get("/api/v1/auth/oauth/google/callback", params=query, follow_redirects=False)
    assert _error_of(replay.headers["location"]) == "invalid_state"
    assert db_session.query(AuthLoginSession).count() == 1


def test_an_expired_state_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    params = _start(client, "google")
    db_session.query(OAuthLoginState).update({"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)})
    db_session.commit()
    provider.response = {"id_token": "unused"}
    response = client.get("/api/v1/auth/oauth/google/callback",
                          params={"state": params["state"], "code": "x"}, follow_redirects=False)
    assert _error_of(response.headers["location"]) == "invalid_state"
    assert provider.token_requests == []   # never reached the provider


def test_a_state_cannot_be_used_with_the_other_provider(client, db_session, provider):
    _person(db_session, "ava@example.test")
    params = _start(client, "google")
    response = client.get("/api/v1/auth/oauth/microsoft/callback",
                          params={"state": params["state"], "code": "x"}, follow_redirects=False)
    assert _error_of(response.headers["location"]) == "invalid_state"


def test_a_provider_error_or_cancelled_sign_in_is_handled(client, db_session, provider):
    _person(db_session, "ava@example.test")
    location, _ = _callback(client, "google", provider, error="access_denied")
    assert _error_of(location) == "provider_error"
    assert "access_denied" in _events(db_session, "login_failed")[0].detail


def test_an_unreachable_provider_is_reported_without_details(client, db_session, provider):
    _person(db_session, "ava@example.test")
    provider.response = httpx.ConnectError("secret-host.internal refused")
    params = _start(client, "google")
    response = client.get("/api/v1/auth/oauth/google/callback",
                          params={"state": params["state"], "code": "x"}, follow_redirects=False)
    assert _error_of(response.headers["location"]) == "provider_unavailable"
    assert "secret-host" not in response.headers["location"]


# ---------------------------------------------------------------------------
# The one-time code, and tokens that never travel in a URL
# ---------------------------------------------------------------------------


def test_only_a_one_time_code_is_in_the_redirect_never_a_token(client, db_session, provider):
    _person(db_session, "ava@example.test")
    location, _ = _callback(client, "google", provider)
    assert location.startswith("http://localhost:5173/auth/callback?code=")
    assert "access_token" not in location and "refresh" not in location and "eyJ" not in location


def test_the_one_time_code_works_once(client, db_session, provider):
    _person(db_session, "ava@example.test")
    location, _ = _callback(client, "google", provider)
    code = parse_qs(urlparse(location).query)["code"][0]
    assert client.post("/api/v1/auth/oauth/exchange", json={"code": code}).status_code == 200
    again = client.post("/api/v1/auth/oauth/exchange", json={"code": code})
    assert again.status_code == 400 and "already" in again.json()["detail"]


def test_the_one_time_code_expires_and_a_made_up_one_fails(client, db_session, provider):
    _person(db_session, "ava@example.test")
    location, _ = _callback(client, "google", provider)
    code = parse_qs(urlparse(location).query)["code"][0]
    db_session.query(OAuthLoginState).update(
        {"exchange_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    db_session.commit()
    assert client.post("/api/v1/auth/oauth/exchange", json={"code": code}).status_code == 400
    assert client.post("/api/v1/auth/oauth/exchange", json={"code": "made-up-code-value"}).status_code == 400


def test_secrets_never_appear_in_responses_or_the_database_in_the_clear(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    # Only hashes of refresh tokens and one-time codes are stored.
    stored = [row.token_hash for row in db_session.query(AuthRefreshToken).all()]
    assert body["refresh_token"] not in stored and all(len(h) == 64 for h in stored)
    assert db_session.query(OAuthLoginState).one().exchange_hash != ""
    for text in (json.dumps(body), client.get("/api/v1/auth/providers").text):
        assert GOOGLE_SECRET not in text and MS_SECRET not in text


# ---------------------------------------------------------------------------
# Access tokens: 15-minute expiry
# ---------------------------------------------------------------------------


def test_an_expired_access_token_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    claims["exp"] = int(time.time()) - 5
    expired = jwt.encode(claims, AUTH_SECRET, algorithm="HS256")
    response = client.get("/api/v1/auth/me", headers=_bearer(expired))
    assert response.status_code == 401 and response.json()["detail"] == "Invalid or expired access token."


def test_the_server_side_expiry_of_an_access_token_is_enforced_too(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    db_session.query(AuthSession).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    db_session.commit()
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 401


def test_an_access_token_cannot_be_used_after_the_account_is_disabled(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    db_session.query(AuthAccount).update({"is_enabled": False})
    db_session.commit()
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 401


# ---------------------------------------------------------------------------
# Refresh-token rotation and reuse detection
# ---------------------------------------------------------------------------


def _refresh(client, token: str):
    return client.post("/api/v1/auth/refresh", json={"refresh_token": token})


def test_refresh_returns_new_tokens_and_the_old_refresh_token_stops_working(client, db_session, provider):
    _person(db_session, "ava@example.test")
    first = _sign_in(client, "google", provider)

    second = _refresh(client, first["refresh_token"])
    assert second.status_code == 200
    renewed = second.json()
    assert renewed["refresh_token"] != first["refresh_token"] and renewed["access_token"] != first["access_token"]
    assert renewed["expires_in"] == 900
    assert client.get("/api/v1/auth/me", headers=_bearer(renewed)).status_code == 200

    # Rotation: the token just used is spent; the next one is good.
    third = _refresh(client, renewed["refresh_token"])
    assert third.status_code == 200
    assert third.json()["refresh_token"] not in (first["refresh_token"], renewed["refresh_token"])


def test_reusing_a_spent_refresh_token_ends_the_whole_session(client, db_session, provider):
    _person(db_session, "ava@example.test")
    first = _sign_in(client, "google", provider)
    renewed = _refresh(client, first["refresh_token"]).json()

    # Someone presents the old token again: it was copied.
    reuse = _refresh(client, first["refresh_token"])
    assert reuse.status_code == 401 and "security" in reuse.json()["detail"]

    # The legitimate holder's newest tokens are dead too.
    assert _refresh(client, renewed["refresh_token"]).status_code == 401
    me = client.get("/api/v1/auth/me", headers=_bearer(renewed))
    assert me.status_code == 401 and "security" in me.json()["detail"]
    detected = _events(db_session, "refresh_reuse_detected")
    assert len(detected) == 1 and detected[0].provider == "google" and detected[0].ip_address == "testclient"


def test_an_unknown_refresh_token_is_rejected(client, db_session, provider):
    assert _refresh(client, "x" * 40).status_code == 401


def test_an_expired_refresh_token_is_rejected(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    db_session.query(AuthRefreshToken).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    db_session.commit()
    response = _refresh(client, body["refresh_token"])
    assert response.status_code == 401 and "expired" in response.json()["detail"]


def test_a_refresh_token_cannot_outlive_its_session(client, db_session, provider, monkeypatch):
    monkeypatch.setenv("AUTH_REFRESH_TOKEN_TTL_SECONDS", "999999")
    monkeypatch.setenv("AUTH_SESSION_MAX_AGE_SECONDS", "600")
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    assert body["refresh_expires_in"] <= 600


def test_refreshing_fails_once_the_account_is_disabled(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    db_session.query(AuthAccount).update({"is_enabled": False})
    db_session.commit()
    response = _refresh(client, body["refresh_token"])
    assert response.status_code == 401 and "no longer active" in response.json()["detail"]


def test_the_refresh_endpoint_rejects_malformed_requests(client):
    assert client.post("/api/v1/auth/refresh", json={}).status_code == 422
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": "short"}).status_code == 422


# ---------------------------------------------------------------------------
# Idle timeout and absolute session lifetime
# ---------------------------------------------------------------------------


def _age_session(db: Session, *, idle: timedelta | None = None, expires_in: timedelta | None = None) -> None:
    values = {}
    if idle is not None:
        values["last_active_at"] = datetime.now(timezone.utc) - idle
    if expires_in is not None:
        values["expires_at"] = datetime.now(timezone.utc) + expires_in
    db.query(AuthLoginSession).update(values)
    db.commit()


def test_an_idle_session_times_out(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 200

    _age_session(db_session, idle=timedelta(minutes=31))
    response = client.get("/api/v1/auth/me", headers=_bearer(body))
    assert response.status_code == 401 and "inactive" in response.json()["detail"]
    session = db_session.query(AuthLoginSession).one()
    assert session.revoked_reason == "idle_timeout"
    timeout = _events(db_session, "session_idle_timeout")
    assert len(timeout) == 1 and "30 minutes" in timeout[0].detail


def test_an_idle_session_cannot_be_refreshed(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    _age_session(db_session, idle=timedelta(minutes=45))
    response = _refresh(client, body["refresh_token"])
    assert response.status_code == 401 and "inactive" in response.json()["detail"]


def test_activity_keeps_a_session_alive(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    _age_session(db_session, idle=timedelta(minutes=29))
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 200   # records activity
    db_session.expire_all()
    assert datetime.now(timezone.utc) - db_session.query(AuthLoginSession).one().last_active_at.replace(
        tzinfo=timezone.utc) < timedelta(minutes=1)
    _age_session(db_session, idle=timedelta(minutes=29))   # another near-miss: still fine
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 200


def test_refreshing_counts_as_activity(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    _age_session(db_session, idle=timedelta(minutes=29))
    assert _refresh(client, body["refresh_token"]).status_code == 200


def test_the_idle_timeout_is_configurable(client, db_session, provider, monkeypatch):
    monkeypatch.setenv("AUTH_IDLE_TIMEOUT_SECONDS", "120")
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    _age_session(db_session, idle=timedelta(minutes=3))
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 401


def test_a_session_ends_at_its_absolute_expiry_however_active(client, db_session, provider):
    _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    _age_session(db_session, expires_in=timedelta(seconds=-1))
    response = client.get("/api/v1/auth/me", headers=_bearer(body))
    assert response.status_code == 401 and "expired" in response.json()["detail"]
    assert _refresh(client, body["refresh_token"]).status_code == 401


# ---------------------------------------------------------------------------
# Login auditing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_key, label", [("google", "Google"), ("microsoft", "Microsoft")])
def test_every_successful_login_is_audited_with_user_provider_time_and_session(
        client, db_session, provider, provider_key, label):
    employee = _person(db_session, "ava@example.test")
    before = datetime.now(timezone.utc) - timedelta(seconds=5)

    _sign_in(client, provider_key, provider)

    [event] = _events(db_session, "login_success")
    session = db_session.query(AuthLoginSession).one()
    assert event.user_id == employee.id and event.provider == provider_key
    assert event.session_id == session.id and event.ip_address == "testclient"
    assert event.created_at.replace(tzinfo=timezone.utc) >= before
    assert f"Signed in with {label}" in event.detail and "testclient" in event.detail
    assert event.user_agent and "testclient" in event.user_agent.lower() or event.user_agent is not None


def test_password_sign_in_is_audited_and_gets_a_session_and_refresh_token(client, db_session, provider):
    employee = _person(db_session, "ava@example.test")
    body = _password_login(client, "ava@example.test")
    assert body["refresh_token"] and body["expires_in"] == 900
    [event] = _events(db_session, "login_success")
    assert event.provider == "password" and event.user_id == employee.id
    assert db_session.query(AuthLoginSession).one().provider == "password"
    assert _refresh(client, body["refresh_token"]).status_code == 200


def test_the_login_shows_up_in_the_admin_audit_log(client, db_session, provider):
    _person(db_session, "ava@example.test")
    admin = _person(db_session, "boss@example.test", role="admin")
    _sign_in(client, "google", provider)
    admin_token = _password_login(client, "boss@example.test")

    response = client.get("/api/v1/admin/audit-logs", params={"source": "auth"}, headers=_bearer(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["source_labels"]["auth"] == "Sign-ins" and body["counts"]["auth"] == 2
    google = next(e for e in body["entries"] if "Google" in e["detail"])
    assert google["action_label"] == "Signed in" and google["outcome"] == "success"
    assert google["actor_name"] == "Ava" and google["source"] == "auth"
    text = response.text
    for secret in (GOOGLE_SECRET, AUTH_SECRET, "refresh_token"):
        assert secret not in text


def test_failed_sign_ins_are_audited_without_any_token_or_code(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _callback(client, "google", provider, aud="wrong", code="very-secret-provider-code")
    [event] = _events(db_session, "login_failed")
    row = " ".join(str(v) for v in (event.detail, event.user_agent, event.ip_address))
    assert "very-secret-provider-code" not in row and "eyJ" not in row


# ---------------------------------------------------------------------------
# Logout and administrator session management
# ---------------------------------------------------------------------------


def test_logout_ends_the_session_everywhere(client, db_session, provider):
    employee = _person(db_session, "ava@example.test")
    body = _sign_in(client, "google", provider)
    assert client.post("/api/v1/auth/logout", headers=_bearer(body)).status_code == 204
    assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 401
    assert _refresh(client, body["refresh_token"]).status_code == 401
    [event] = _events(db_session, "logout")
    assert event.user_id == employee.id


def test_only_an_administrator_can_see_or_revoke_sessions(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _person(db_session, "mgr@example.test", role="manager")
    employee = _password_login(client, "ava@example.test")
    manager = _password_login(client, "mgr@example.test")
    session_id = db_session.query(AuthLoginSession).first().id
    for token in (employee, manager):
        assert client.get("/api/v1/admin/sessions", headers=_bearer(token)).status_code == 403
        assert client.delete(f"/api/v1/admin/sessions/{session_id}", headers=_bearer(token)).status_code == 403
        assert client.delete("/api/v1/admin/users/emp-ava/sessions", headers=_bearer(token)).status_code == 403
    assert client.get("/api/v1/admin/sessions").status_code == 401


def test_admin_lists_active_sessions_with_who_how_and_when(client, db_session, provider):
    ava = _person(db_session, "ava@example.test")
    _person(db_session, "boss@example.test", role="admin")
    _sign_in(client, "microsoft", provider)
    admin = _password_login(client, "boss@example.test")

    body = client.get("/api/v1/admin/sessions", headers=_bearer(admin)).json()

    assert body["idle_timeout_seconds"] == 1800
    sessions = {s["user"]["email"]: s for s in body["sessions"]}
    assert set(sessions) == {"ava@example.test", "boss@example.test"}
    ava_session = sessions["ava@example.test"]
    assert ava_session["user"] == {"id": ava.id, "name": "Ava", "email": "ava@example.test", "role": "employee"}
    assert ava_session["provider"] == "microsoft" and ava_session["provider_label"] == "Microsoft"
    assert ava_session["ip_address"] == "testclient" and ava_session["is_current"] is False
    assert {"signed_in_at", "last_active_at", "expires_at", "device"} <= set(ava_session)
    assert sessions["boss@example.test"]["is_current"] is True
    assert sessions["boss@example.test"]["provider"] == "password"
    text = json.dumps(body)
    for private in ("token", "password_hash", "code_verifier", "nonce"):
        assert private not in text


def test_the_session_list_leaves_out_ended_sessions(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _person(db_session, "boss@example.test", role="admin")
    _sign_in(client, "google", provider)
    admin = _password_login(client, "boss@example.test")
    _age_session(db_session, idle=timedelta(minutes=1))
    db_session.query(AuthLoginSession).filter(AuthLoginSession.provider == "google").update(
        {"last_active_at": datetime.now(timezone.utc) - timedelta(hours=2)})
    db_session.commit()
    emails = [s["user"]["email"] for s in client.get("/api/v1/admin/sessions", headers=_bearer(admin)).json()["sessions"]]
    assert emails == ["boss@example.test"]


def test_revoking_a_session_ends_its_access_and_refresh_tokens_at_once(client, db_session, provider):
    ava = _person(db_session, "ava@example.test")
    boss = _person(db_session, "boss@example.test", role="admin")
    user = _sign_in(client, "google", provider)
    admin = _password_login(client, "boss@example.test")
    session_id = db_session.query(AuthLoginSession).filter(AuthLoginSession.provider == "google").one().id

    revoked = client.delete(f"/api/v1/admin/sessions/{session_id}", headers=_bearer(admin))
    assert revoked.status_code == 200 and revoked.json() == {"id": session_id, "user_id": ava.id, "revoked": True}

    me = client.get("/api/v1/auth/me", headers=_bearer(user))
    assert me.status_code == 401 and "administrator" in me.json()["detail"]
    refreshed = _refresh(client, user["refresh_token"])
    assert refreshed.status_code == 401 and "administrator" in refreshed.json()["detail"]
    # The administrator's own session is untouched, and the revoked one is gone from the list.
    assert client.get("/api/v1/auth/me", headers=_bearer(admin)).status_code == 200
    listed = client.get("/api/v1/admin/sessions", headers=_bearer(admin)).json()["sessions"]
    assert [s["user"]["email"] for s in listed] == ["boss@example.test"]
    [event] = _events(db_session, "session_revoked")
    assert event.actor_id == boss.id and event.user_id == ava.id and event.session_id == session_id


def test_revoking_twice_is_harmless_and_an_unknown_session_is_a_404(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _person(db_session, "boss@example.test", role="admin")
    _sign_in(client, "google", provider)
    admin = _password_login(client, "boss@example.test")
    session_id = db_session.query(AuthLoginSession).filter(AuthLoginSession.provider == "google").one().id
    assert client.delete(f"/api/v1/admin/sessions/{session_id}", headers=_bearer(admin)).status_code == 200
    assert client.delete(f"/api/v1/admin/sessions/{session_id}", headers=_bearer(admin)).status_code == 200
    assert len(_events(db_session, "session_revoked")) == 1
    assert client.delete("/api/v1/admin/sessions/does-not-exist", headers=_bearer(admin)).status_code == 404


def test_an_administrator_can_sign_someone_out_everywhere(client, db_session, provider):
    ava = _person(db_session, "ava@example.test")
    _person(db_session, "boss@example.test", role="admin")
    google = _sign_in(client, "google", provider)
    password = _password_login(client, "ava@example.test")
    admin = _password_login(client, "boss@example.test")

    result = client.delete(f"/api/v1/admin/users/{ava.id}/sessions", headers=_bearer(admin))
    assert result.status_code == 200 and result.json() == {"revoked": 2}
    for body in (google, password):
        assert client.get("/api/v1/auth/me", headers=_bearer(body)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_bearer(admin)).status_code == 200


def test_revoked_session_access_is_denied_and_the_attempt_is_not_an_unhandled_error(client, db_session, provider):
    _person(db_session, "ava@example.test")
    _person(db_session, "boss@example.test", role="admin")
    user = _sign_in(client, "google", provider)
    admin = _password_login(client, "boss@example.test")
    session_id = db_session.query(AuthLoginSession).filter(AuthLoginSession.provider == "google").one().id
    client.delete(f"/api/v1/admin/sessions/{session_id}", headers=_bearer(admin))
    for path in ("/api/v1/auth/me", "/api/v1/admin/sessions", "/api/v1/admin/dashboard"):
        response = client.get(path, headers=_bearer(user))
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


def test_old_tokens_without_a_login_session_still_work(client, db_session, provider):
    """Tokens issued before Story 10.1 have no login session and must keep working."""
    from app.auth_service import issue_access_token

    employee = _person(db_session, "ava@example.test")
    account = db_session.query(AuthAccount).one()
    legacy = issue_access_token(db_session, account, employee)
    response = client.get("/api/v1/auth/me", headers=_bearer(legacy.token))
    assert response.status_code == 200
