"""Login, OAuth sign-in (Google / Microsoft), token refresh and session endpoints.

Accounts are provisioned by operators; signing in with Google or Microsoft links to an
existing account and never creates one (Story 10.1).
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth_oauth import (
    OAuthLoginError,
    available_providers,
    complete_callback,
    exchange_login_code,
    frontend_base_url,
    start_authorization,
)
from app.auth_service import (
    AuthConfigurationError,
    AuthenticatedPrincipal,
    authenticate_password,
    revoke_access_token,
)
from app.auth_schemas import (
    AuthenticatedUserResponse,
    LoginRequest,
    LoginResponse,
    OAuthExchangeRequest,
    OAuthProvidersResponse,
    OAuthStartResponse,
    RefreshRequest,
)
from app.auth_sessions import SessionEnded, SessionTokens, end_own_session, rotate_refresh_token, start_session
from app.authentication import require_authenticated_principal
from app.db.database import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

OAUTH_PROVIDERS = ("google", "microsoft")


def _user_response(principal: AuthenticatedPrincipal) -> AuthenticatedUserResponse:
    return AuthenticatedUserResponse(
        id=principal.user_id,
        account_id=principal.account_id,
        name=principal.name,
        email=principal.email,
        role=principal.role,
        team=principal.team,
    )


def _client_details(request: Request) -> tuple[str | None, str | None]:
    host = request.client.host if request.client else None
    return host, request.headers.get("user-agent")


def _login_response(tokens: SessionTokens) -> LoginResponse:
    return LoginResponse(
        access_token=tokens.access.token,
        expires_in=tokens.access.expires_in,
        refresh_token=tokens.refresh_token,
        refresh_expires_in=tokens.refresh_expires_in,
        user=_user_response(tokens.access.principal),
    )


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication is not configured.",
    )


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    try:
        account_employee = authenticate_password(db, payload.email, payload.password)
        if account_employee is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        account, employee = account_employee
        ip_address, user_agent = _client_details(request)
        tokens = start_session(
            db, account, employee, "password", ip_address=ip_address, user_agent=user_agent
        )
    except AuthConfigurationError as exc:
        raise _not_configured() from exc
    return _login_response(tokens)


@router.get("/providers", response_model=OAuthProvidersResponse)
def oauth_providers():
    """Which "Sign in with …" buttons to show. Never returns an id, secret or URL."""
    return OAuthProvidersResponse(providers=available_providers(), password_enabled=True)


@router.post("/oauth/{provider}/start", response_model=OAuthStartResponse)
def oauth_start(provider: str, db: Session = Depends(get_db)):
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown sign-in provider.")
    try:
        return OAuthStartResponse(authorization_url=start_authorization(db, provider))
    except OAuthLoginError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.message) from error


@router.get("/oauth/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """Where the provider sends the browser back. Always answers with a redirect to the app."""
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown sign-in provider.")
    ip_address, user_agent = _client_details(request)
    try:
        exchange_code = complete_callback(
            db, provider, code=code, state=state, provider_error=error,
            ip_address=ip_address, user_agent=user_agent,
        )
        target = f"{frontend_base_url()}/auth/callback?{urlencode({'code': exchange_code})}"
    except OAuthLoginError as failure:
        target = f"{frontend_base_url()}/login?{urlencode({'error': failure.code})}"
    except AuthConfigurationError:
        target = f"{frontend_base_url()}/login?{urlencode({'error': 'not_configured'})}"
    return RedirectResponse(
        target,
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/oauth/exchange", response_model=LoginResponse)
def oauth_exchange(payload: OAuthExchangeRequest, db: Session = Depends(get_db)):
    try:
        tokens = exchange_login_code(db, payload.code)
    except OAuthLoginError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=error.message
        ) from error
    except AuthConfigurationError as exc:
        raise _not_configured() from exc
    return _login_response(tokens)


@router.post("/refresh", response_model=LoginResponse)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    """Swap a refresh token for a new access token and a new refresh token."""
    ip_address, user_agent = _client_details(request)
    try:
        tokens = rotate_refresh_token(
            db, payload.refresh_token, ip_address=ip_address, user_agent=user_agent
        )
    except SessionEnded as ended:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ended.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from ended
    except AuthConfigurationError as exc:
        raise _not_configured() from exc
    return _login_response(tokens)


@router.get("/me", response_model=AuthenticatedUserResponse)
def current_user(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
):
    return _user_response(principal)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    revoke_access_token(db, principal)
    end_own_session(db, principal.session_id, user_id=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
