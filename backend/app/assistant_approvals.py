"""Approval workflow for assistant writes that are not low risk (Story 6.6).

``execute_tool`` pauses medium- and high-risk writes as an
``AssistantActionApproval`` with status "pending" ("Approval Required"):

* medium risk — ``approver_kind="requester"``: the requester confirms it;
* high risk   — ``approver_kind="approver"``: someone else decides. HR / Operations
  and admins may approve anyone's request; a manager may approve requests from
  employees in their own team. Nobody approves their own high-risk request.

Approving runs the action through ``execute_tool`` again — so permissions,
conflicts and risk are re-checked at that moment — using the *requester's*
principal, rebuilt from the employee directory and their login account. The
approver's own (possibly broader) privileges are never used for the action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.assistant_tools import (
    ToolError,
    ToolExecutionOutcome,
    ToolNotFound,
    ToolPermissionDenied,
    ToolApprovalError,
    approval_as_dict,
    execute_tool,
    record_audit,
)
from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import (
    ACTION_APPROVAL_STATUS_CANCELLED,
    ACTION_APPROVAL_STATUS_EXPIRED,
    ACTION_APPROVAL_STATUS_PENDING,
    ACTION_APPROVAL_STATUS_REJECTED,
    AssistantActionApproval,
    AuthAccount,
    Employee,
)

HIGH_RISK_APPROVER_ROLES = frozenset({"hr_operations", "admin"})


@dataclass
class ApprovalDecisionResult:
    approval: AssistantActionApproval
    outcome: ToolExecutionOutcome | None
    message: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _same_team(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def expire_stale(db: Session) -> int:
    """Mark pending approvals past their deadline as expired (audited)."""
    now = _now()
    stale = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
            AssistantActionApproval.expires_at <= now,
        )
        .all()
    )
    for approval in stale:
        approval.status = ACTION_APPROVAL_STATUS_EXPIRED
        approval.updated_at = now
    db.commit()
    for approval in stale:
        from app.assistant_run_tracing import resolve_run_for_approval

        resolve_run_for_approval(db, approval, status="failed", output_data={"status": "expired"})
        record_audit(
            db, actor_id=approval.requester_id, actor_role=approval.requester_role,
            tool_name=approval.tool_name, event="expired", risk_level=approval.risk_level,
            approval_id=approval.id, conversation_id=approval.conversation_id,
            detail="The approval request expired before it was approved; nothing was run.",
        )
    return len(stale)


def requester_principal(db: Session, approval: AssistantActionApproval) -> AuthenticatedPrincipal:
    """The requester as they are *now*: current employee record and login role.

    A requester who was disabled or demoted since asking cannot have the action
    run with the permissions they used to have.
    """
    employee = db.get(Employee, approval.requester_id)
    if employee is None or not employee.is_active:
        raise ToolPermissionDenied(
            "The requester is no longer an active employee, so the action cannot run "
            "with their permissions."
        )
    account = db.query(AuthAccount).filter(AuthAccount.employee_id == employee.id).one_or_none()
    if account is not None and not account.is_enabled:
        raise ToolPermissionDenied(
            "The requester's login is disabled, so the action cannot run with their permissions."
        )
    return AuthenticatedPrincipal(
        user_id=employee.id,
        account_id=account.id if account else "assistant-approval",
        email=employee.email,
        name=employee.name,
        # Directory accounts carry the live role; without one, use the role
        # recorded when the request was made (never anything broader).
        role=account.role if account else approval.requester_role,
        team=employee.team,
        jti="assistant-approval",
    )


def decision_block_reason(
    approval: AssistantActionApproval,
    decider: AuthenticatedPrincipal,
) -> str | None:
    """Why ``decider`` may not decide this approval, or None when they may."""
    if approval.status != ACTION_APPROVAL_STATUS_PENDING:
        return f"This request is already {approval.status}."
    if _as_aware(approval.expires_at) <= _now():
        return "This request has expired."
    if approval.approver_kind == "requester":
        if decider.user_id != approval.requester_id:
            return "Only the person who made this medium-risk request can confirm it."
        return None
    if decider.user_id == approval.requester_id:
        return "You cannot approve your own high-risk request; HR / Operations, an admin, or your manager must."
    if decider.role in HIGH_RISK_APPROVER_ROLES:
        return None
    if (
        decider.role == "manager"
        and approval.requester_role == "employee"
        and _same_team(decider.team, approval.requester_team)
    ):
        return None
    return (
        "Only HR / Operations, an admin, or the manager of the requester's team can "
        "approve this high-risk request."
    )


def can_decide(approval: AssistantActionApproval, decider: AuthenticatedPrincipal) -> bool:
    return decision_block_reason(approval, decider) is None


def get_approval(db: Session, approval_id: str) -> AssistantActionApproval:
    approval = db.get(AssistantActionApproval, approval_id)
    if approval is None:
        raise ToolNotFound("Approval request not found.")
    return approval


def decide_approval(
    db: Session,
    approval_id: str,
    decider: AuthenticatedPrincipal,
    decision: str,
    note: str | None = None,
    *,
    continue_run: bool = False,
) -> ApprovalDecisionResult:
    """Approve (and run as the requester) or reject one pending request."""
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'.")
    expire_stale(db)
    approval = get_approval(db, approval_id)
    blocked = decision_block_reason(approval, decider)
    if blocked:
        record_audit(
            db, actor=decider, tool_name=approval.tool_name, event="approval_denied",
            risk_level=approval.risk_level, approval_id=approval.id,
            conversation_id=approval.conversation_id, detail=blocked,
        )
        raise ToolPermissionDenied(blocked) if approval.status == ACTION_APPROVAL_STATUS_PENDING \
            else ToolApprovalError(blocked)

    now = _now()
    approval.decided_by = decider.user_id
    approval.decided_by_role = decider.role
    approval.decided_at = now
    approval.decision_note = (note or "").strip() or None
    approval.updated_at = now
    if decision == "reject":
        approval.status = ACTION_APPROVAL_STATUS_REJECTED
        db.commit()
        from app.assistant_run_tracing import resolve_run_for_approval

        resolve_run_for_approval(db, approval, status="cancelled", output_data={"status": "rejected"})
        record_audit(
            db, actor=decider, tool_name=approval.tool_name, event="rejected",
            risk_level=approval.risk_level, approval_id=approval.id,
            conversation_id=approval.conversation_id,
            detail=f"Rejected by {decider.name} ({decider.role})"
                   + (f": {approval.decision_note}" if approval.decision_note else "."),
        )
        from app.notification_service import notify_assistant_approval_decision

        notify_assistant_approval_decision(db, approval, outcome="rejected")
        return ApprovalDecisionResult(approval, None, "Rejected. Nothing was run.")

    db.commit()
    record_audit(
        db, actor=decider, tool_name=approval.tool_name, event="approved",
        risk_level=approval.risk_level, approval_id=approval.id,
        conversation_id=approval.conversation_id,
        detail=f"Approved by {decider.name} ({decider.role}); running with the "
               "requester's permissions.",
    )
    try:
        requester = requester_principal(db, approval)
        outcome = execute_tool(
            db,
            requester,
            approval.tool_name,
            dict(approval.arguments),
            idempotency_key=approval.idempotency_key,
            conversation_id=approval.conversation_id,
            approval_id=approval.id,
        )
    except ToolError as exc:
        db.refresh(approval)
        if approval.status == ACTION_APPROVAL_STATUS_PENDING:
            # Denied or blocked on the re-check, before anything ran.
            approval.status = "failed"
            approval.error = exc.message[:2000]
            approval.updated_at = _now()
            db.commit()
            record_audit(
                db, actor_id=approval.requester_id, actor_role=approval.requester_role,
                tool_name=approval.tool_name, event="execution_failed",
                risk_level=approval.risk_level, approval_id=approval.id,
                conversation_id=approval.conversation_id, detail=exc.message,
            )
        if approval.status == "failed":
            from app.assistant_run_tracing import resolve_run_for_approval

            resolve_run_for_approval(
                db,
                approval,
                status="failed",
                output_data={"status": "failed"},
            )
            from app.notification_service import notify_assistant_approval_decision

            notify_assistant_approval_decision(db, approval, outcome="failed")
        return ApprovalDecisionResult(approval, None, f"Approved, but it could not run: {exc.message}")
    db.refresh(approval)
    from app.assistant_run_tracing import resolve_run_for_approval

    resolve_run_for_approval(
        db,
        approval,
        status="completed",
        execution_id=outcome.execution_id,
        output_data={"status": "executed"},
        finalize_run=not continue_run,
    )
    from app.notification_service import notify_assistant_approval_decision

    notify_assistant_approval_decision(db, approval, outcome="executed")
    return ApprovalDecisionResult(approval, outcome, "Approved and done.")


def cancel_approval(
    db: Session,
    approval_id: str,
    actor: AuthenticatedPrincipal,
) -> AssistantActionApproval:
    approval = get_approval(db, approval_id)
    if approval.requester_id != actor.user_id:
        raise ToolPermissionDenied("Only the requester can cancel this request.")
    if approval.status != ACTION_APPROVAL_STATUS_PENDING:
        raise ToolApprovalError(f"This request is already {approval.status}.")
    approval.status = ACTION_APPROVAL_STATUS_CANCELLED
    approval.updated_at = _now()
    db.commit()
    from app.assistant_run_tracing import resolve_run_for_approval

    resolve_run_for_approval(db, approval, status="cancelled", output_data={"status": "cancelled"})
    record_audit(
        db, actor=actor, tool_name=approval.tool_name, event="cancelled",
        risk_level=approval.risk_level, approval_id=approval.id,
        conversation_id=approval.conversation_id,
        detail="Cancelled by the requester; nothing was run.",
    )
    return approval


def list_approvals(
    db: Session,
    actor: AuthenticatedPrincipal,
    *,
    scope: str,
    status: str | None,
    limit: int = 50,
) -> list[AssistantActionApproval]:
    """``scope="mine"``: the actor's own requests; ``"queue"``: those they may decide."""
    expire_stale(db)
    query = db.query(AssistantActionApproval)
    if status:
        query = query.filter(AssistantActionApproval.status == status)
    query = query.order_by(AssistantActionApproval.created_at.desc())
    if scope == "mine":
        return query.filter(AssistantActionApproval.requester_id == actor.user_id).limit(limit).all()
    candidates = query.filter(
        AssistantActionApproval.approver_kind == "approver",
        AssistantActionApproval.requester_id != actor.user_id,
    ).limit(500).all()
    visible = []
    for approval in candidates:
        if actor.role in HIGH_RISK_APPROVER_ROLES or (
            actor.role == "manager"
            and approval.requester_role == "employee"
            and _same_team(actor.team, approval.requester_team)
        ):
            visible.append(approval)
    return visible[:limit]


def approval_view(approval: AssistantActionApproval, viewer: AuthenticatedPrincipal, db: Session) -> dict:
    requester = db.get(Employee, approval.requester_id)
    decider = db.get(Employee, approval.decided_by) if approval.decided_by else None
    return {
        **approval_as_dict(approval),
        "requester_name": requester.name if requester else approval.requester_id,
        "requester_role": approval.requester_role,
        "requester_team": approval.requester_team,
        "arguments": approval.arguments,
        "decided_by": approval.decided_by,
        "decided_by_name": decider.name if decider else None,
        "decided_at": _as_aware(approval.decided_at).isoformat() if approval.decided_at else None,
        "decision_note": approval.decision_note,
        "execution_id": approval.execution_id,
        "result": approval.result,
        "error": approval.error,
        "conversation_id": approval.conversation_id,
        "created_at": _as_aware(approval.created_at).isoformat(),
        "can_decide": can_decide(approval, viewer),
        "can_cancel": approval.requester_id == viewer.user_id
        and approval.status == ACTION_APPROVAL_STATUS_PENDING,
    }
