"""Verified Bearer-token dependencies shared by protected API routes."""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth_service import (
    AuthConfigurationError,
    AuthenticatedPrincipal,
    InvalidAccessToken,
    authenticate_access_token,
)
from app.db.database import get_db

_BEARER = HTTPBearer(auto_error=False)


def get_authenticated_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    db: Session = Depends(get_db),
) -> AuthenticatedPrincipal | None:
    if credentials is None:
        request.state.authenticated_principal = None
        return None
    if credentials.scheme.casefold() != "bearer":
        request.state.authenticated_principal = None
        return None
    try:
        principal = authenticate_access_token(db, credentials.credentials)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured.",
        ) from exc
    except InvalidAccessToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    request.state.authenticated_principal = principal
    return principal


def authenticated_actor_id(
    principal: AuthenticatedPrincipal | None = Depends(get_authenticated_principal),
) -> str | None:
    return principal.user_id if principal else None


def require_authenticated_principal(
    principal: AuthenticatedPrincipal | None = Depends(get_authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_authenticated_actor_id(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> str:
    return principal.user_id