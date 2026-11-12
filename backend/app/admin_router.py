"""Administrator-only dashboard API (Story 9.4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.access_audit import deny_access
from app.admin_assistant_runs import (
    DEFAULT_LIMIT as ASSISTANT_RUN_DEFAULT_LIMIT,
    MAX_LIMIT as ASSISTANT_RUN_MAX_LIMIT,
    assistant_run_detail,
    list_assistant_runs,
)
from app.admin_audit_service import MAX_LIMIT, list_audit_entries
from app.admin_dashboard_service import build_dashboard
from app.admin_workflow_runs import list_workflow_runs, workflow_run_detail
from app.auth_sessions import (
    SessionNotFound,
    list_active_sessions,
    revoke_session_as_admin,
    revoke_user_sessions_as_admin,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    ASSISTANT_RUN_STATUSES,
    AUTH_ACCOUNT_ROLES,
    WORKFLOW_RUN_STATUSES,
    AuthAccount,
    AuthAuditEvent,
    Employee,
)

router = APIRouter(prefix="/api/v1/admin", tags=["Administration"])


class DepartmentUpdateRequest(BaseModel):
    department: str = Field(min_length=1, max_length=100)


class RoleUpdateRequest(BaseModel):
    role: str = Field(min_length=1, max_length=30)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in AUTH_ACCOUNT_ROLES:
            allowed = ", ".join(AUTH_ACCOUNT_ROLES)
            raise ValueError(f"role must be one of: {allowed}.")
        return value


def require_administrator(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator can view the system dashboard.",
        )
    return principal


def require_admin_users_access(
    request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal.role != "admin":
        deny_access(
            db,
            request,
            principal=principal,
            detail="Only an administrator can view employee and department data.",
            resource="admin.users.read",
        )
    return principal


def require_user_management_administrator(
    request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal.role != "admin":
        deny_access(
            db,
            request,
            principal=principal,
            detail="Only an administrator can update employee access settings.",
        )
    return principal


@router.get("/dashboard")
def get_admin_dashboard(
    response: Response,
    _: AuthenticatedPrincipal = Depends(require_admin_users_access),
    db: Session = Depends(get_db),
):
    # Live operational data: never let a browser or proxy serve a stale copy.
    response.headers["Cache-Control"] = "no-store"
    return build_dashboard(db)


@router.patch("/users/{employee_id}/department")
def update_employee_department(
    employee_id: str,
    body: DepartmentUpdateRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_user_management_administrator),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    department = body.department.strip()
    if not department:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Department must not be blank.",
        )

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found.",
        )

    previous_department = employee.department
    if previous_department != department:
        employee.department = department
        db.add(
            AuthAuditEvent(
                id=str(uuid.uuid4()),
                event="department_updated",
                user_id=employee.id,
                actor_id=principal.user_id,
                session_id=principal.session_id,
                detail=(
                    f"Department changed from "
                    f"{previous_department or 'Unassigned'} to {department}."
                ),
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    return {"employee_id": employee.id, "department": employee.department}


@router.patch("/users/{employee_id}/role")
def update_employee_account_role(
    employee_id: str,
    body: RoleUpdateRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_user_management_administrator),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found.",
        )

    account = (
        db.query(AuthAccount)
        .filter(AuthAccount.employee_id == employee.id)
        .first()
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee login account not found.",
        )

    previous_role = account.role
    if previous_role != body.role:
        account.role = body.role
        db.add(
            AuthAuditEvent(
                id=str(uuid.uuid4()),
                event="role_updated",
                user_id=employee.id,
                actor_id=principal.user_id,
                session_id=principal.session_id,
                detail=f"Account role changed from {previous_role} to {body.role}.",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    return {"employee_id": employee.id, "role": account.role}


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


# ---------------------------------------------------------------------------
# Story 10.1 - sign-in sessions
# ---------------------------------------------------------------------------


@router.get("/sessions")
def get_active_sessions(
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """Everyone who is signed in right now: who, how, from where, and when last active."""
    response.headers["Cache-Control"] = "no-store"
    return list_active_sessions(db, current_session_id=principal.session_id)


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: str,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """End one session. Its access and refresh tokens stop working immediately."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return revoke_session_as_admin(db, session_id, admin_id=principal.user_id)
    except SessionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.") from error


@router.delete("/users/{employee_id}/sessions")
def revoke_all_user_sessions(
    employee_id: str,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_administrator),
    db: Session = Depends(get_db),
):
    """Sign one person out everywhere."""
    response.headers["Cache-Control"] = "no-store"
    return {"revoked": revoke_user_sessions_as_admin(db, employee_id, admin_id=principal.user_id)}
