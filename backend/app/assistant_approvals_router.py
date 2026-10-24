"""HTTP API for assistant action approvals and the assistant audit log (Story 6.6).

  GET  /api/v1/assistant/approvals?scope=mine|queue&status=   requests made / decidable
  GET  /api/v1/assistant/approvals/{id}                       one request (requester or decider)
  POST /api/v1/assistant/approvals/{id}/decide                approve (runs as the requester) or reject
  POST /api/v1/assistant/approvals/{id}/cancel                requester withdraws a pending request
  GET  /api/v1/assistant/audit                                permission, conflict, risk and approval decisions
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.assistant_approvals import (
    HIGH_RISK_APPROVER_ROLES,
    approval_view,
    can_decide,
    cancel_approval,
    decide_approval,
    get_approval,
    list_approvals,
)
from app.assistant_tools import ToolError
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import ACTION_APPROVAL_STATUSES, AssistantActionApproval, AssistantAuditEvent

router = APIRouter(prefix="/api/v1/assistant", tags=["AI Assistant Approvals"])


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=2000)


def _raise(exc: ToolError):
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _visible(approval: AssistantActionApproval, principal: AuthenticatedPrincipal) -> bool:
    return approval.requester_id == principal.user_id or can_decide(approval, principal) or (
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
    approvals = list_approvals(db, principal, scope=scope, status=status or None, limit=limit)
    return [approval_view(approval, principal, db) for approval in approvals]


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
    if not _visible(approval, principal):
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
        result = decide_approval(db, approval_id, principal, req.decision, req.note)
    except ToolError as exc:
        _raise(exc)
    return {
        "message": result.message,
        "approval": approval_view(result.approval, principal, db),
        "execution": result.outcome.as_dict() if result.outcome else None,
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
