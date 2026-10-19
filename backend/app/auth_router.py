"""Local login and session endpoints; accounts are provisioned by operators."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth_service import (
    AuthConfigurationError,
    AuthenticatedPrincipal,
    authenticate_password,
    issue_access_token,
    revoke_access_token,
)
from app.auth_schemas import AuthenticatedUserResponse, LoginRequest, LoginResponse
from app.authentication import require_authenticated_principal
from app.db.database import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def _user_response(principal: AuthenticatedPrincipal) -> AuthenticatedUserResponse:
    return AuthenticatedUserResponse(
        id=principal.user_id,
        account_id=principal.account_id,
        name=principal.name,
        email=principal.email,
        role=principal.role,
        team=principal.team,
    )


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        account_employee = authenticate_password(db, payload.email, payload.password)
        if account_employee is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        account, employee = account_employee
        token = issue_access_token(db, account, employee)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured.",
        ) from exc

    return LoginResponse(
        access_token=token.token,
        expires_in=token.expires_in,
        user=_user_response(token.principal),
    )


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
    return Response(status_code=status.HTTP_204_NO_CONTENT)