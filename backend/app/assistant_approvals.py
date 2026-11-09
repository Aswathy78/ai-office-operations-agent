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
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import uuid

import jwt
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.assistant_tools import (
    ToolError,
    ToolExecutionOutcome,
    ToolArgumentError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolApprovalError,
    arguments_hash,
    approval_as_dict,
    execute_tool,
    get_registry,
    record_audit,
    validate_against_schema,
)
from app.auth_service import AuthenticatedPrincipal, _jwt_settings
from app.meeting.models import (
    ACTION_APPROVAL_STATUS_CANCELLED,
    ACTION_APPROVAL_STATUS_EXPIRED,
    ACTION_APPROVAL_STATUS_EXECUTING,
    ACTION_APPROVAL_STATUS_PENDING,
    ACTION_APPROVAL_STATUS_REJECTED,
    AssistantApprovalEmailLink,
    AssistantActionApproval,
    AuthAccount,
    Employee,
)

HIGH_RISK_APPROVER_ROLES = frozenset({"hr_operations", "admin"})
logger = logging.getLogger(__name__)


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


def _current_requester_team(db: Session, approval: AssistantActionApproval) -> str | None:
    requester = db.get(Employee, approval.requester_id)
    return requester.team if requester is not None and requester.is_active else None


def effective_expiration_at(approval: AssistantActionApproval) -> datetime:
    """Keep even older pending requests within Story 9.7's 48-hour decision window."""
    stored_expiration = _as_aware(approval.expires_at)
    story_deadline = _as_aware(approval.created_at) + timedelta(hours=48)
    return min(stored_expiration, story_deadline)


def expire_stale(db: Session, *, now: datetime | None = None) -> int:
    """Expire overdue requests and retry any requester expiry notices not yet recorded."""
    now = _as_aware(now or _now())
    deadline_floor = now - timedelta(hours=48)
    pending = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
            or_(
                AssistantActionApproval.expires_at <= now,
                AssistantActionApproval.created_at <= deadline_floor,
            ),
        )
        .all()
    )
    stale = []
    for approval in pending:
        if effective_expiration_at(approval) > now:
            continue
        claimed = (
            db.query(AssistantActionApproval)
            .filter(
                AssistantActionApproval.id == approval.id,
                AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
            )
            .update(
                {
                    AssistantActionApproval.status: ACTION_APPROVAL_STATUS_EXPIRED,
                    AssistantActionApproval.decided_at: now,
                    AssistantActionApproval.decided_by_role: "system",
                    AssistantActionApproval.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if not claimed:
            continue
        approval.status = ACTION_APPROVAL_STATUS_EXPIRED
        approval.decided_at = now
        approval.decided_by_role = "system"
        approval.updated_at = now
        stale.append(approval)
    db.commit()
    for approval in stale:
        from app.assistant_run_tracing import resolve_run_for_approval

        resolve_run_for_approval(db, approval, status="failed", output_data={"status": "expired"})
        record_audit(
            db, actor_id="system", actor_role="system",
            tool_name=approval.tool_name, event="expired", risk_level=approval.risk_level,
            approval_id=approval.id, conversation_id=approval.conversation_id,
            detail="The approval request expired before it was approved; nothing was run.",
        )
    expired_without_notice = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_EXPIRED,
            AssistantActionApproval.expiry_notified_at.is_(None),
        )
        .all()
    )
    for approval in expired_without_notice:
        claimed = (
            db.query(AssistantActionApproval)
            .filter(
                AssistantActionApproval.id == approval.id,
                AssistantActionApproval.expiry_notified_at.is_(None),
            )
            .update(
                {AssistantActionApproval.expiry_notified_at: now},
                synchronize_session=False,
            )
        )
        if not claimed:
            continue
        db.commit()
        try:
            from app.notification_service import notify_assistant_approval_decision

            notify_assistant_approval_decision(db, approval, outcome="expired")
        except Exception:
            db.query(AssistantActionApproval).filter(
                AssistantActionApproval.id == approval.id,
                AssistantActionApproval.expiry_notified_at == now,
            ).update(
                {AssistantActionApproval.expiry_notified_at: None},
                synchronize_session=False,
            )
            db.commit()
            logger.exception("Could not notify requester that approval %s expired", approval.id)
            continue
    return len(stale)


def process_approval_reminders(db: Session, *, now: datetime | None = None) -> int:
    """Send one idempotent reminder for each high-risk request pending at 24 hours."""
    current_time = _as_aware(now or _now())
    expire_stale(db, now=current_time)
    candidates = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
            AssistantActionApproval.approver_kind == "approver",
            AssistantActionApproval.reminder_sent_at.is_(None),
        )
        .all()
    )
    sent = 0
    for approval in candidates:
        if (
            _as_aware(approval.created_at) + timedelta(hours=24) > current_time
            or effective_expiration_at(approval) <= current_time
        ):
            continue
        updated = (
            db.query(AssistantActionApproval)
            .filter(
                AssistantActionApproval.id == approval.id,
                AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
                AssistantActionApproval.reminder_sent_at.is_(None),
            )
            .update(
                {
                    AssistantActionApproval.reminder_sent_at: current_time,
                    AssistantActionApproval.updated_at: current_time,
                },
                synchronize_session=False,
            )
        )
        if updated:
            db.commit()
            try:
                from app.notification_service import notify_assistant_approval_reminder

                notified = notify_assistant_approval_reminder(db, approval)
            except Exception:
                db.query(AssistantActionApproval).filter(
                    AssistantActionApproval.id == approval.id,
                    AssistantActionApproval.reminder_sent_at == current_time,
                ).update(
                    {AssistantActionApproval.reminder_sent_at: None},
                    synchronize_session=False,
                )
                db.commit()
                raise
            if notified:
                sent += 1
            else:
                db.query(AssistantActionApproval).filter(
                    AssistantActionApproval.id == approval.id,
                    AssistantActionApproval.reminder_sent_at == current_time,
                ).update(
                    {AssistantActionApproval.reminder_sent_at: None},
                    synchronize_session=False,
                )
                db.commit()
    return sent


def assign_alternate_approver(
    db: Session,
    approval_id: str,
    actor: AuthenticatedPrincipal,
    alternate_email: str | None,
) -> AssistantActionApproval:
    """Assign a manager or organization approver to one pending request."""
    if actor.role not in HIGH_RISK_APPROVER_ROLES:
        raise ToolPermissionDenied("Only HR / Operations or an admin can assign an alternate approver.")
    expire_stale(db)
    approval = get_approval(db, approval_id)
    if approval.status != ACTION_APPROVAL_STATUS_PENDING or approval.approver_kind != "approver":
        raise ToolApprovalError("An alternate approver can only be assigned to a pending high-risk request.")

    alternate = None
    if alternate_email and alternate_email.strip():
        alternate_account = (
            db.query(AuthAccount)
            .filter(
                AuthAccount.email_normalized == alternate_email.strip().casefold(),
                AuthAccount.is_enabled.is_(True),
                AuthAccount.role.in_({"manager", "hr_operations", "admin"}),
            )
            .one_or_none()
        )
        alternate = (
            db.get(Employee, alternate_account.employee_id)
            if alternate_account is not None
            else None
        )
        if alternate is None or not alternate.is_active:
            raise ToolArgumentError(
                "The alternate must be an active manager, HR / Operations approver, or admin."
            )
        if alternate.id == approval.requester_id:
            raise ToolArgumentError("The requester cannot be assigned as their own alternate approver.")
        if alternate.id == approval.alternate_approver_id:
            return approval

    previous_id = approval.alternate_approver_id
    assigned_id = alternate.id if alternate else None
    updated = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.id == approval.id,
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
        )
        .update(
            {
                AssistantActionApproval.alternate_approver_id: assigned_id,
                AssistantActionApproval.updated_at: _now(),
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise ToolApprovalError("This request has already been decided.")
    db.refresh(approval)
    record_audit(
        db,
        actor=actor,
        tool_name=approval.tool_name,
        event="alternate_approver_assigned" if alternate else "alternate_approver_cleared",
        risk_level=approval.risk_level,
        approval_id=approval.id,
        conversation_id=approval.conversation_id,
        detail=(
            f"Alternate approver {alternate.name} assigned."
            if alternate
            else f"Alternate approver assignment {previous_id or ''} cleared."
        ),
        data={"alternate_approver_id": alternate.id if alternate else None},
    )
    if alternate is not None:
        from app.notification_service import notify_assistant_alternate_approver

        notify_assistant_alternate_approver(db, approval, alternate)
    return approval


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
    db: Session,
) -> str | None:
    """Why ``decider`` may not decide this approval, or None when they may."""
    if approval.status != ACTION_APPROVAL_STATUS_PENDING:
        return f"This request is already {approval.status}."
    if effective_expiration_at(approval) <= _now():
        return "This request has expired."
    if approval.approver_kind == "requester":
        if decider.user_id != approval.requester_id:
            return "Only the person who made this medium-risk request can confirm it."
        return None
    if decider.user_id == approval.requester_id:
        return "You cannot approve your own high-risk request; HR / Operations, an admin, or your manager must."
    if approval.alternate_approver_id == decider.user_id:
        if decider.role in {"manager", *HIGH_RISK_APPROVER_ROLES}:
            return None
        return "The assigned alternate must be a manager, HR / Operations approver, or admin."
    if decider.role in HIGH_RISK_APPROVER_ROLES:
        return None
    if (
        decider.role == "manager"
        and approval.requester_role == "employee"
        and _same_team(decider.team, _current_requester_team(db, approval))
    ):
        return None
    return (
        "Only HR / Operations, an admin, or the manager of the requester's team can "
        "approve this high-risk request."
    )


def can_decide(
    approval: AssistantActionApproval,
    decider: AuthenticatedPrincipal,
    db: Session,
) -> bool:
    return decision_block_reason(approval, decider, db) is None


def get_approval(db: Session, approval_id: str) -> AssistantActionApproval:
    approval = db.get(AssistantActionApproval, approval_id)
    if approval is None:
        raise ToolNotFound("Approval request not found.")
    return approval


def issue_approval_email_link(
    db: Session,
    approval: AssistantActionApproval,
    recipient_id: str,
) -> str:
    """Create a recipient-bound signed link and persist its one-use digest."""
    secret, issuer, audience, _ = _jwt_settings()
    now = _now()
    expires_at = min(effective_expiration_at(approval), now + timedelta(hours=24))
    if expires_at <= now:
        raise ToolApprovalError("This approval request has expired.")
    link_id = str(uuid.uuid4())
    token = jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": recipient_id,
            "aid": approval.id,
            "jti": link_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        },
        secret,
        algorithm="HS256",
    )
    db.add(
        AssistantApprovalEmailLink(
            id=link_id,
            approval_id=approval.id,
            recipient_id=recipient_id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=expires_at,
            created_at=now,
        )
    )
    db.commit()
    return token


def verify_approval_email_link(
    db: Session,
    token: str,
    principal: AuthenticatedPrincipal,
) -> tuple[AssistantApprovalEmailLink, AssistantActionApproval]:
    secret, issuer, audience, _ = _jwt_settings()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "jti", "sub", "aid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ToolApprovalError("This approval email link has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise ToolApprovalError("This approval email link is invalid.") from exc

    link = db.get(AssistantApprovalEmailLink, claims["jti"])
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if (
        link is None
        or link.approval_id != claims["aid"]
        or link.recipient_id != claims["sub"]
        or not hmac.compare_digest(link.token_hash, digest)
    ):
        raise ToolApprovalError("This approval email link is invalid.")
    if link.recipient_id != principal.user_id:
        raise ToolPermissionDenied("This approval email link was issued to another user.")
    if link.used_at is not None:
        raise ToolApprovalError("This approval email link has already been used.")
    if _as_aware(link.expires_at) <= _now():
        raise ToolApprovalError("This approval email link has expired.")
    approval = get_approval(db, link.approval_id)
    blocked = decision_block_reason(approval, principal, db)
    if blocked:
        raise ToolApprovalError(blocked)
    return link, approval


def consume_approval_email_link(
    db: Session,
    link: AssistantApprovalEmailLink,
) -> None:
    now = _now()
    updated = (
        db.query(AssistantApprovalEmailLink)
        .filter(
            AssistantApprovalEmailLink.id == link.id,
            AssistantApprovalEmailLink.used_at.is_(None),
            AssistantApprovalEmailLink.expires_at > now,
        )
        .update(
            {AssistantApprovalEmailLink.used_at: now},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise ToolApprovalError("This approval email link has expired or has already been used.")
    db.commit()


def decide_approval(
    db: Session,
    approval_id: str,
    decider: AuthenticatedPrincipal,
    decision: str,
    note: str | None = None,
    *,
    arguments: dict | None = None,
    continue_run: bool = False,
) -> ApprovalDecisionResult:
    """Approve (and run as the requester) or reject one pending request."""
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'.")
    expire_stale(db)
    approval = get_approval(db, approval_id)
    blocked = decision_block_reason(approval, decider, db)
    if blocked:
        record_audit(
            db, actor=decider, tool_name=approval.tool_name, event="approval_denied",
            risk_level=approval.risk_level, approval_id=approval.id,
            conversation_id=approval.conversation_id, detail=blocked,
        )
        raise ToolPermissionDenied(blocked) if approval.status == ACTION_APPROVAL_STATUS_PENDING \
            else ToolApprovalError(blocked)

    now = _now()
    spec = get_registry().get(approval.tool_name)
    new_arguments = dict(approval.arguments)
    new_affected_people = approval.affected_people
    if decision == "approve":
        new_arguments = dict(arguments if arguments is not None else approval.arguments)
        if approval.tool_name == "email.send":
            groups = new_arguments.get("groups", [])
            requester = requester_principal(db, approval)
            authorization_arguments = dict(new_arguments)
            frozen_recipients = [
                item for item in (approval.affected_people or []) if isinstance(item, dict)
            ]
            if any(item.get("source") == "Everyone" for item in frozen_recipients):
                frozen_emails = {
                    item.get("email") for item in frozen_recipients if item.get("email")
                }
                current_emails = set(new_arguments.get("recipient_emails", []))
                if arguments is None or current_emails == frozen_emails:
                    authorization_arguments["groups"] = ["Everyone"]
            if spec.authorize is not None:
                denials = spec.authorize(db, requester, authorization_arguments)
                if denials:
                    raise ToolPermissionDenied(
                        "Permission denied: " + " ".join(denials), denials
                    )
            if groups and not approval.affected_people and arguments is None:
                raise ToolApprovalError(
                    "This older approval does not contain a frozen recipient list. "
                    "Edit the parameters to specify recipient emails, or reject it and "
                    "ask the requester to submit it again."
                )
            if groups and not approval.affected_people and arguments is not None:
                raise ToolApprovalError(
                    "This older approval does not contain a frozen recipient list. "
                    "Remove directory groups and specify the recipients explicitly before approval."
                )
            if arguments is not None or new_affected_people is None:
                from app.email_draft_service import resolve_email_send_recipients

                try:
                    resolved_arguments, new_affected_people = resolve_email_send_recipients(
                        db,
                        recipient_emails=new_arguments.get("recipient_emails", []),
                        groups=groups,
                        exclude_email=requester.email,
                    )
                except ValueError as exc:
                    raise ToolArgumentError(str(exc)) from exc
                new_arguments["recipient_emails"] = resolved_arguments["recipient_emails"]
                if "groups" in new_arguments:
                    new_arguments["groups"] = []
            elif approval.affected_people is not None:
                frozen_emails = [
                    item["email"]
                    for item in approval.affected_people
                    if isinstance(item, dict) and isinstance(item.get("email"), str)
                ]
                if not frozen_emails:
                    raise ToolApprovalError(
                        "This approval's frozen recipient list is invalid. "
                        "Reject it and ask the requester to submit it again."
                    )
                new_arguments["recipient_emails"] = frozen_emails
                new_arguments["groups"] = []
        errors = validate_against_schema(new_arguments, spec.input_schema)
        if not errors and spec.validator is not None:
            errors = spec.validator(new_arguments)
        if errors:
            message = "; ".join(
                f"{error['path']} {error['message']}" for error in errors[:5]
            )
            raise ToolArgumentError(
                f"Invalid arguments for {approval.tool_name!r}: {message}", errors
            )

    changed_arguments = (
        decision == "approve"
        and arguments is not None
        and arguments_hash(new_arguments) != approval.arguments_hash
    )
    execution_key = (
        f"approval-edit:{approval.id}:{arguments_hash(new_arguments)}"
        if changed_arguments
        else approval.idempotency_key
    )
    new_status = (
        ACTION_APPROVAL_STATUS_REJECTED
        if decision == "reject"
        else ACTION_APPROVAL_STATUS_EXECUTING
    )
    values = {
        "status": new_status,
        "decided_by": decider.user_id,
        "decided_by_role": decider.role,
        "decided_at": now,
        "decision_note": (note or "").strip() or None,
        "updated_at": now,
    }
    if decision == "approve":
        values.update(
            {
                "arguments": new_arguments,
                "affected_people": new_affected_people,
                "arguments_hash": arguments_hash(new_arguments),
                "idempotency_key": execution_key,
                "summary": spec.summarize_call(new_arguments),
            }
        )
    claimed = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.id == approval.id,
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
        )
        .update(values, synchronize_session=False)
    )
    if claimed != 1:
        db.rollback()
        raise ToolApprovalError("This request has already been decided.")
    db.commit()
    db.refresh(approval)

    if decision == "reject":
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
            new_arguments,
            idempotency_key=execution_key,
            conversation_id=approval.conversation_id,
            approval_id=approval.id,
        )
    except ToolError as exc:
        db.refresh(approval)
        if approval.status == ACTION_APPROVAL_STATUS_EXECUTING:
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
    except Exception:
        logger.exception("Unexpected failure executing approved action %s", approval.id)
        db.refresh(approval)
        if approval.status == ACTION_APPROVAL_STATUS_EXECUTING:
            approval.status = "failed"
            approval.error = "The action failed unexpectedly. Check the audit log for details."
            approval.updated_at = _now()
            db.commit()
            record_audit(
                db,
                actor_id=approval.requester_id,
                actor_role=approval.requester_role,
                tool_name=approval.tool_name,
                event="execution_failed",
                risk_level=approval.risk_level,
                approval_id=approval.id,
                conversation_id=approval.conversation_id,
                detail=approval.error,
            )
        from app.assistant_run_tracing import resolve_run_for_approval

        resolve_run_for_approval(
            db, approval, status="failed", output_data={"status": "failed"}
        )
        from app.notification_service import notify_assistant_approval_decision

        notify_assistant_approval_decision(db, approval, outcome="failed")
        raise
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
    expire_stale(db)
    approval = get_approval(db, approval_id)
    if approval.requester_id != actor.user_id:
        raise ToolPermissionDenied("Only the requester can cancel this request.")
    if approval.status != ACTION_APPROVAL_STATUS_PENDING:
        raise ToolApprovalError(f"This request is already {approval.status}.")
    approval.status = ACTION_APPROVAL_STATUS_CANCELLED
    approval.decided_by = actor.user_id
    approval.decided_by_role = actor.role
    approval.decided_at = _now()
    approval.updated_at = approval.decided_at
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
    candidates_query = query.filter(
        AssistantActionApproval.approver_kind == "approver",
        AssistantActionApproval.requester_id != actor.user_id,
    )
    if actor.role in HIGH_RISK_APPROVER_ROLES:
        return candidates_query.limit(limit).all()
    if actor.role != "manager":
        return []
    assigned = candidates_query.filter(
        AssistantActionApproval.alternate_approver_id == actor.user_id
    ).limit(limit).all()
    ordinary = candidates_query.filter(
        AssistantActionApproval.alternate_approver_id.is_(None)
        | (AssistantActionApproval.alternate_approver_id != actor.user_id)
    ).limit(500).all()
    requester_teams: dict[str, str | None] = {}
    if ordinary:
        requester_teams = dict(
            db.query(Employee.id, Employee.team)
            .filter(
                Employee.id.in_({approval.requester_id for approval in ordinary}),
                Employee.is_active.is_(True),
            )
            .all()
        )
    visible = assigned + [
        approval
        for approval in ordinary
        if approval.requester_role == "employee"
        and _same_team(actor.team, requester_teams.get(approval.requester_id))
    ]
    return list({approval.id: approval for approval in visible}.values())[:limit]


def approval_view(approval: AssistantActionApproval, viewer: AuthenticatedPrincipal, db: Session) -> dict:
    requester = db.get(Employee, approval.requester_id)
    decider = db.get(Employee, approval.decided_by) if approval.decided_by else None
    alternate = db.get(Employee, approval.alternate_approver_id) if approval.alternate_approver_id else None
    affected_recipients: list[dict] = []
    recipient_snapshot_missing = False
    if approval.tool_name == "email.send":
        if approval.affected_people:
            affected_recipients = [
                {
                    "name": item.get("name") or item["email"],
                    "email": item["email"],
                    "source": item.get("source"),
                }
                for item in approval.affected_people
                if isinstance(item, dict) and isinstance(item.get("email"), str)
            ]
        else:
            recipient_snapshot_missing = bool(approval.arguments.get("groups"))
            if not recipient_snapshot_missing:
                for email in approval.arguments.get("recipient_emails", []):
                    employee = (
                        db.query(Employee)
                        .filter(Employee.email.ilike(email))
                        .one_or_none()
                    )
                    affected_recipients.append(
                        {
                            "name": employee.name if employee else email,
                            "email": email,
                            "source": "Existing request",
                        }
                    )
    else:
        affected_people = []
        people_keys = (
            "assignee", "attendee", "employee", "manager", "member", "owner",
            "person", "recipient", "user",
        )

        def collect_people(value, key: str = "") -> None:
            normalized = key.casefold()
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    child_name = str(child_key)
                    if not any(fragment in child_name.casefold() for fragment in people_keys):
                        child_name = key
                    collect_people(child_value, child_name)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    collect_people(item, key)
                return
            if not any(fragment in normalized for fragment in people_keys):
                return
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                employee = (
                    db.query(Employee)
                    .filter(
                        (Employee.id == candidate)
                        | (Employee.email.ilike(candidate))
                    )
                    .one_or_none()
                )
                affected_people.append(employee.name if employee else candidate)

        collect_people(approval.arguments)
        affected_people = list(dict.fromkeys(affected_people))
    affected_people = [
        f"{item['name']} <{item['email']}>"
        if item["name"] != item["email"]
        else item["email"]
        for item in affected_recipients
    ] if approval.tool_name == "email.send" else affected_people
    return {
        **approval_as_dict(approval),
        "expires_at": effective_expiration_at(approval).isoformat(),
        "requester_name": requester.name if requester else approval.requester_id,
        "requester_role": approval.requester_role,
        "requester_team": (
            requester.team
            if requester and requester.is_active
            else approval.requester_team
        ),
        "arguments": approval.arguments,
        "affected_people": affected_people,
        "affected_recipients": affected_recipients,
        "recipient_snapshot_missing": recipient_snapshot_missing,
        "source": {
            "type": "AI Assistant",
            "conversation_id": approval.conversation_id,
        },
        "decided_by": approval.decided_by,
        "decided_by_name": decider.name if decider else None,
        "decided_at": _as_aware(approval.decided_at).isoformat() if approval.decided_at else None,
        "history_actor_name": (
            decider.name
            if decider
            else "System"
            if approval.status == ACTION_APPROVAL_STATUS_EXPIRED
            else requester.name
            if approval.status == ACTION_APPROVAL_STATUS_CANCELLED and requester
            else None
        ),
        "history_at": _as_aware(
            approval.decided_at or approval.updated_at or approval.created_at
        ).isoformat(),
        "alternate_approver_id": approval.alternate_approver_id,
        "alternate_approver_name": alternate.name if alternate else None,
        "alternate_approver_email": alternate.email if alternate else None,
        "reminder_sent_at": (
            _as_aware(approval.reminder_sent_at).isoformat()
            if approval.reminder_sent_at else None
        ),
        "decision_note": approval.decision_note,
        "execution_id": approval.execution_id,
        "result": approval.result,
        "error": approval.error,
        "conversation_id": approval.conversation_id,
        "created_at": _as_aware(approval.created_at).isoformat(),
        "can_decide": can_decide(approval, viewer, db),
        "can_cancel": approval.requester_id == viewer.user_id
        and approval.status == ACTION_APPROVAL_STATUS_PENDING,
    }
