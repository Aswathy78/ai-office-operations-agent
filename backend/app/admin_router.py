"""Administrator-only dashboard API (Story 9.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.admin_assistant_runs import (
    DEFAULT_LIMIT as ASSISTANT_RUN_DEFAULT_LIMIT,
    MAX_LIMIT as ASSISTANT_RUN_MAX_LIMIT,
    assistant_run_detail,
    list_assistant_runs,
)
from app.admin_audit_service import MAX_LIMIT, list_audit_entries
from app.admin_dashboard_service import build_dashboard
from app.admin_workflow_runs import list_workflow_runs, workflow_run_detail
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import ASSISTANT_RUN_STATUSES, WORKFLOW_RUN_STATUSES

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


@router.get("/assistant-runs")
def get_assistant_runs(
    response: Response,
    run_status: str | None = Query(default=None, alias="status"),
    user: str | None = Query(default=None, min_length=1, max_length=100),
    limit: int = Query(default=ASSISTANT_RUN_DEFAULT_LIMIT, ge=1, le=ASSISTANT_RUN_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """List interactive assistant runs; user search matches ID, name or e-mail."""
    response.headers["Cache-Control"] = "no-store"
    if run_status is not None and run_status not in ASSISTANT_RUN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of: {', '.join(ASSISTANT_RUN_STATUSES)}.",
        )
    return list_assistant_runs(
        db,
        status=run_status,
        user=user,
        limit=limit,
        offset=offset,
    )


@router.get("/assistant-runs/{run_id}")
def get_assistant_run_detail(
    run_id: str,
    response: Response,
    _: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """Inspect one run without exposing conversation, checkpoint, or approval payloads."""
    response.headers["Cache-Control"] = "no-store"
    detail = assistant_run_detail(db, run_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant run not found.",
        )
    return detail


@router.get("/workflow-runs")
def get_workflow_runs(
    response: Response,
    run_status: str | None = Query(default=None, alias="status"),
    user: str | None = Query(default=None, min_length=1, max_length=100),
    limit: int = Query(default=ASSISTANT_RUN_DEFAULT_LIMIT, ge=1, le=ASSISTANT_RUN_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """List background workflow records without returning payloads or checkpoints."""
    response.headers["Cache-Control"] = "no-store"
    if run_status is not None and run_status not in WORKFLOW_RUN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of: {', '.join(WORKFLOW_RUN_STATUSES)}.",
        )
    return list_workflow_runs(
        db,
        status=run_status,
        user=user,
        limit=limit,
        offset=offset,
    )


@router.get("/workflow-runs/{run_id}")
def get_workflow_run_detail(
    run_id: str,
    response: Response,
    _: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """Return safe workflow lifecycle and step metadata, never checkpoints or payloads."""
    response.headers["Cache-Control"] = "no-store"
    detail = workflow_run_detail(db, run_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow run not found.",
        )
    return detail
