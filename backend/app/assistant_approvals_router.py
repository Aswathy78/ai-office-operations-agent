"""HTTP API for assistant action approvals and the assistant audit log (Story 6.6).

  GET  /api/v1/assistant/approvals?scope=mine|queue&status=   requests made / decidable
  GET  /api/v1/assistant/approvals/history                   HR / Operations decision history
  GET  /api/v1/assistant/approvals/{id}                       one request (requester or decider)
  POST /api/v1/assistant/approvals/{id}/decide                approve (runs as the requester) or reject
  PUT  /api/v1/assistant/approvals/{id}/alternate-approver    assign an alternate approver
  POST /api/v1/assistant/approvals/{id}/cancel                requester withdraws a pending request
  GET  /api/v1/assistant/audit                                permission, conflict, risk and approval decisions
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased

from app.assistant_approvals import (
    HIGH_RISK_APPROVER_ROLES,
    approval_view,
    assign_alternate_approver,
    can_decide,
    cancel_approval,
    consume_approval_email_link,
    decide_approval,
    get_approval,
    list_approvals,
    verify_approval_email_link,
)
from app.assistant_tools import ToolError
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    ACTION_APPROVAL_STATUSES,
    AssistantActionApproval,
    AssistantAuditEvent,
    Employee,
)

router = APIRouter(prefix="/api/v1/assistant", tags=["AI Assistant Approvals"])


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=2000)
    arguments: dict | None = None


class EmailApprovalDecisionRequest(ApprovalDecisionRequest):
    token: str = Field(min_length=1, max_length=8192)


class EmailApprovalPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=8192)


class AlternateApproverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=320)


def _raise(exc: ToolError):
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _visible(
    approval: AssistantActionApproval,
    principal: AuthenticatedPrincipal,
    db: Session,
) -> bool:
    return approval.requester_id == principal.user_id or can_decide(approval, principal, db) or (
        principal.role in HIGH_RISK_APPROVER_ROLES
    )


@router.get("/approvals")
def get_approvals(
    scope: Literal["mine", "queue"] = Query(default="mine"),
    status: str | None = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    if status and status not in ACTION_APPROVAL_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status {status!r}.")
    if scope == "queue" and principal.role not in {"manager", "hr_operations", "admin"}:
        raise HTTPException(status_code=403, detail="Only managers and operations approvers can view the approval queue.")
    approvals = list_approvals(db, principal, scope=scope, status=status or None, limit=limit)
    return [approval_view(approval, principal, db) for approval in approvals]


@router.get("/approvals/history")
def get_approval_history(
    status: str | None = Query(default=None),
    approver: str | None = Query(default=None, max_length=320),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Return decision records to HR / Operations and admins with audit filters."""
    if principal.role not in HIGH_RISK_APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="Only HR / Operations and admins can view approval history.")
    if status and status not in ACTION_APPROVAL_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status {status!r}.")
    if from_date and to_date and to_date < from_date:
        raise HTTPException(status_code=422, detail="to_date must be on or after from_date.")

    decider = aliased(Employee)
    history_at = func.coalesce(
        AssistantActionApproval.decided_at,
        AssistantActionApproval.updated_at,
        AssistantActionApproval.created_at,
    )
    query = (
        db.query(AssistantActionApproval)
        .outerjoin(decider, decider.id == AssistantActionApproval.decided_by)
        .filter(
            AssistantActionApproval.status.in_(
                {"executed", "rejected", "cancelled", "expired", "failed"}
            )
        )
    )
    if status:
        query = query.filter(AssistantActionApproval.status == status)
    if approver and approver.strip():
        pattern = f"%{approver.strip()}%"
        query = query.filter(
            or_(
                decider.id == approver.strip(),
                decider.name.ilike(pattern),
                decider.email.ilike(pattern),
                and_(
                    approver.strip().casefold() == "system",
                    AssistantActionApproval.status == "expired",
                    AssistantActionApproval.decided_by.is_(None),
                ),
            )
        )
    if from_date:
        start = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        query = query.filter(history_at >= start)
    if to_date:
        end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        query = query.filter(history_at < end)
    approvals = (
        query.order_by(history_at.desc())
        .limit(limit)
        .all()
    )
    return [approval_view(approval, principal, db) for approval in approvals]


@router.put("/approvals/{approval_id}/alternate-approver")
def put_alternate_approver(
    approval_id: str,
    req: AlternateApproverRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        approval = assign_alternate_approver(db, approval_id, principal, req.email)
    except ToolError as exc:
        _raise(exc)
    return approval_view(approval, principal, db)


@router.post("/approvals/email-preview")
def preview_email_approval(
    req: EmailApprovalPreviewRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        _, approval = verify_approval_email_link(db, req.token, principal)
    except ToolError as exc:
        _raise(exc)
    response.headers["Cache-Control"] = "no-store"
    return approval_view(approval, principal, db)


@router.get("/approvals/{approval_id}")
def get_approval_detail(
    approval_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        approval = get_approval(db, approval_id)
    except ToolError as exc:
        _raise(exc)
    if not _visible(approval, principal, db):
        raise HTTPException(status_code=404, detail="Approval request not found.")
    return approval_view(approval, principal, db)


@router.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: str,
    req: ApprovalDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        result = decide_approval(
            db,
            approval_id,
            principal,
            req.decision,
            req.note,
            arguments=req.arguments,
            continue_run=True,
        )
    except ToolError as exc:
        _raise(exc)
    continuation = _continue_approved_run(db, result)
    return {
        "message": result.message,
        "approval": approval_view(result.approval, principal, db),
        "execution": result.outcome.as_dict() if result.outcome else None,
        "continuation": continuation,
    }


@router.post("/approvals/email-decision")
def decide_from_email_link(
    req: EmailApprovalDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        link, approval = verify_approval_email_link(db, req.token, principal)
        result = decide_approval(
            db,
            approval.id,
            principal,
            req.decision,
            req.note,
            arguments=req.arguments,
            continue_run=True,
        )
        consume_approval_email_link(db, link)
    except ToolError as exc:
        _raise(exc)
    return {
        "message": result.message,
        "approval": approval_view(result.approval, principal, db),
        "execution": result.outcome.as_dict() if result.outcome else None,
        "continuation": _continue_approved_run(db, result),
    }


def _continue_approved_run(db: Session, result) -> dict | None:
    if result.outcome is None or result.approval.status != "executed":
        return None
    from app.assistant_router import resume_approved_assistant_run

    return {
        "resumed": resume_approved_assistant_run(db, result.approval, result.outcome),
    }


@router.post("/approvals/{approval_id}/cancel")
def cancel(
    approval_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        approval = cancel_approval(db, approval_id, principal)
    except ToolError as exc:
        _raise(exc)
    return approval_view(approval, principal, db)


@router.get("/audit")
def get_audit_events(
    approval_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Your own audit events; HR / Operations and admins see everyone's."""
    query = db.query(AssistantAuditEvent)
    if principal.role not in HIGH_RISK_APPROVER_ROLES:
        own_approvals = db.query(AssistantActionApproval.id).filter(
            AssistantActionApproval.requester_id == principal.user_id
        )
        query = query.filter(
            (AssistantAuditEvent.actor_id == principal.user_id)
            | AssistantAuditEvent.approval_id.in_(own_approvals)
        )
    if approval_id:
        query = query.filter(AssistantAuditEvent.approval_id == approval_id)
    events = query.order_by(AssistantAuditEvent.created_at.desc()).limit(limit).all()
    return [
        {
            "id": event.id,
            "actor_id": event.actor_id,
            "actor_role": event.actor_role,
            "tool_name": event.tool_name,
            "event": event.event,
            "risk_level": event.risk_level,
            "approval_id": event.approval_id,
            "execution_id": event.execution_id,
            "conversation_id": event.conversation_id,
            "detail": event.detail,
            "data": event.data,
            "created_at": event.created_at.isoformat(),
        }
        for event in events
    ]
