"""Administrator-only dashboard API (Story 9.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.admin_audit_service import MAX_LIMIT, list_audit_entries
from app.admin_dashboard_service import build_dashboard
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db

router = APIRouter(prefix="/api/v1/admin", tags=["Administration"])


def require_administrator(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator can view the system dashboard.",
        )
    return principal


@router.get("/dashboard")
def get_admin_dashboard(
    response: Response,
    _: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    # Live operational data: never let a browser or proxy serve a stale copy.
    response.headers["Cache-Control"] = "no-store"
    return build_dashboard(db)


@router.get("/audit-logs")
def get_audit_logs(
    response: Response,
    source: str | None = Query(default=None, description="task, meeting, assistant, email or workflow"),
    q: str | None = Query(default=None, max_length=100, description="Search the action and detail text"),
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    _: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return list_audit_entries(db, source=source or None, query=q, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
