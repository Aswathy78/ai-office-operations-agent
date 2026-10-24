"""Read-back verification and mismatch escalation for assistant writes (Story 6.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import re
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import (
    AssistantToolExecution,
    EmailDraft,
    Employee,
    Meeting,
    MeetingParticipant,
    Task,
)

logger = logging.getLogger(__name__)
MAX_VERIFICATION_READS = 3
VERIFIED_TOOL_NAMES = frozenset({"task.create", "meeting.schedule", "email.send"})


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _read_back(db: Session, tool_name: str, result: dict) -> dict | None:
    if tool_name == "task.create":
        task = db.get(Task, result.get("task_id"))
        if task is None:
            return None
        assignee = db.get(Employee, task.assignee_id)
        return {
            "id": task.id,
            "title": task.title,
            "assignee_id": task.assignee_id,
            "assignee_email": assignee.email.casefold() if assignee else None,
            "due_at": _aware(task.due_at).isoformat(),
            "priority": task.priority,
            "assignment_email_status": task.assignment_email_status,
        }
    if tool_name == "meeting.schedule":
        meeting = db.get(Meeting, result.get("meeting_id"))
        if meeting is None:
            return None
        participants = (
            db.query(MeetingParticipant.email)
            .filter(MeetingParticipant.meeting_id == meeting.id)
            .all()
        )
        return {
            "id": meeting.id,
            "title": meeting.title,
            "start_at": _aware(meeting.start_at).isoformat(),
            "end_at": _aware(meeting.end_at).isoformat(),
            "attendee_emails": sorted(
                email.casefold() for (email,) in participants if email
            ),
            "join_url": meeting.join_url,
        }
    if tool_name == "email.send":
        draft = db.get(EmailDraft, result.get("draft_id"))
        if draft is None:
            return None
        return {
            "id": draft.id,
            "subject": draft.subject,
            "body": draft.body,
            "recipients": sorted(
                recipient["email"].casefold() for recipient in draft.recipients
            ),
            "status": draft.status,
            "delivery_results": draft.delivery_results or [],
        }
    return None


def _requested_task_title(value: str) -> str:
    """Remove the task-command wrapper accidentally included by chat extraction."""
    title = value.strip()
    wrapped = re.fullmatch(r"""task\s+(["'])(.+)\1""", title, flags=re.IGNORECASE)
    return wrapped.group(2).strip() if wrapped else title


def _expected(
    db: Session,
    principal: AuthenticatedPrincipal,
    tool_name: str,
    arguments: dict,
    result: dict,
) -> dict:
    if tool_name == "task.create":
        assignee = (
            db.get(Employee, arguments["assignee_id"])
            if arguments.get("assignee_id")
            else db.query(Employee).filter(
                Employee.email.ilike(arguments["assignee_email"])
            ).one_or_none()
        )
        due_at = datetime.fromisoformat(arguments["due_at"].replace("Z", "+00:00"))
        return {
            "id": result.get("task_id"),
            "title": _requested_task_title(arguments["title"]),
            "assignee_id": assignee.id if assignee else None,
            "assignee_email": assignee.email.casefold() if assignee else None,
            "due_at": _aware(due_at).isoformat(),
            "priority": arguments.get("priority", "medium"),
        }
    if tool_name == "meeting.schedule":
        start = datetime.fromisoformat(arguments["start_at"].replace("Z", "+00:00"))
        return {
            "id": result.get("meeting_id"),
            "title": arguments["title"].strip(),
            "start_at": _aware(start).isoformat(),
            "end_at": _aware(start + timedelta(minutes=arguments["duration_minutes"])).isoformat(),
            "attendee_emails": sorted(
                {email.strip().casefold() for email in arguments["attendee_emails"]}
            ),
        }
    from app.email_draft_service import _active_employees, build_recipients

    employees_by_email = {
        employee.email.casefold(): employee for employee in _active_employees(db)
    }
    requested_emails = arguments.get("recipient_emails", [])
    named = [
        employees_by_email[email.casefold()]
        for email in requested_emails
        if email.casefold() in employees_by_email
    ]
    raw = [
        email for email in requested_emails
        if email.casefold() not in employees_by_email
    ]
    recipients, _, _ = build_recipients(
        db,
        groups=arguments.get("groups", []),
        employees=named,
        emails=raw,
        exclude_email=principal.email,
    )
    return {
        "id": result.get("draft_id"),
        "subject": arguments["subject"],
        "body": arguments["body"],
        "recipients": sorted(recipient["email"].casefold() for recipient in recipients),
    }


def _mismatches(expected: dict, actual: dict | None) -> list[str]:
    if actual is None:
        return ["The created record could not be read back."]
    return [
        f"{field} did not match the requested value."
        for field, expected_value in expected.items()
        if actual.get(field) != expected_value
    ]


def _links(tool_name: str, result: dict) -> dict:
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:5173").strip().rstrip("/")
    if tool_name == "task.create":
        task_id = result["task_id"]
        path = f"/tasks?{urlencode({'task_id': task_id})}"
        url = f"{base}{path}"
    elif tool_name == "meeting.schedule":
        path = f"/meetings/{result['meeting_id']}"
        url = f"{base}{path}"
    else:
        path = f"/api/v1/email-drafts/{result['draft_id']}"
        api_base = os.getenv(
            "BACKEND_BASE_URL", "http://127.0.0.1:8000"
        ).strip().rstrip("/")
        url = f"{api_base}{path}"
    return {"path": path, "url": url}


def _escalate(
    db: Session,
    principal: AuthenticatedPrincipal,
    tool_name: str,
    execution_id: str,
    mismatches: list[str],
    link: dict,
) -> list[dict]:
    from app.task_service import deliver_email

    recipients: dict[str, str] = {}
    if principal.email:
        recipients[principal.email.casefold()] = principal.email
    requester = db.get(Employee, principal.user_id)
    manager = db.get(Employee, requester.manager_id) if requester and requester.manager_id else None
    if manager and manager.is_active and manager.email:
        recipients[manager.email.casefold()] = manager.email

    delivery_results = []
    for email in recipients.values():
        try:
            message_id = deliver_email(
                email,
                f"Assistant action needs review: {tool_name}",
                "The assistant could not verify a completed action after a retry and "
                "re-planned read-back. Please review it before taking further action.\n\n"
                + "\n".join(mismatches)
                + f"\n\nRecord: {link['url']}\nExecution: {execution_id}",
            )
            delivery_results.append({"recipient": email, "status": "sent", "message_id": message_id})
        except Exception as exc:  # provider implementations may expose different transport errors
            logger.exception("Could not send assistant verification escalation to %s", email)
            delivery_results.append({"recipient": email, "status": "failed", "error": str(exc)[:500]})
    return delivery_results


def verify_write_result(
    db: Session,
    principal: AuthenticatedPrincipal,
    tool_name: str,
    arguments: dict,
    result: dict,
    *,
    execution_id: str,
    conversation_id: str | None,
) -> dict:
    """Verify persisted writes; retry/re-plan read-back only, never repeat the write."""
    if tool_name not in VERIFIED_TOOL_NAMES:
        return result

    expected = _expected(db, principal, tool_name, arguments, result)
    attempts = []
    actual = None
    mismatches = []
    for attempt in range(1, MAX_VERIFICATION_READS + 1):
        if attempt > 1:
            db.expire_all()
        if attempt == MAX_VERIFICATION_READS:
            # Re-plan from the original arguments and current directory state,
            # then perform one final targeted read-back without repeating effects.
            expected = _expected(db, principal, tool_name, arguments, result)
        actual = _read_back(db, tool_name, result)
        mismatches = _mismatches(expected, actual)
        attempts.append({"attempt": attempt, "mismatches": mismatches})
        if not mismatches:
            break

    link = _links(tool_name, result)
    verified = not mismatches
    verification = {
        "status": "verified" if verified else "escalated",
        "verified": verified,
        "attempts": attempts,
        "replanned": len(attempts) == MAX_VERIFICATION_READS,
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
        "link": link,
    }
    result = dict(result)
    result["verification"] = verification
    result["links"] = {"record": link}
    result["step_status"] = "done" if verified else "escalated"
    if tool_name == "task.create":
        result["notifications"] = [{
            "type": "task_assignment_email",
            "status": (
                "not_requested" if not arguments.get("notify_assignee", True)
                else result.get("assignment_email_status", "unknown")
            ),
            "recipient": actual.get("assignee_email") if actual else None,
        }]
    elif tool_name == "meeting.schedule":
        result["notifications"] = [{
            "type": "calendar_invitation",
            "sent": result.get("invitations_sent", []),
            "failed": result.get("invitation_failures", []),
        }]
    else:
        if actual is not None:
            result["delivery_results"] = actual["delivery_results"]
        result["notifications"] = [{
            "type": "email_delivery",
            "status": result.get("outcome", result.get("status", "unknown")),
            "recipients": result.get("recipients", []),
        }]

    escalation = []
    if not verified:
        escalation = _escalate(
            db, principal, tool_name, execution_id, mismatches, link
        )
        verification["notifications"] = escalation

    from app.assistant_tools import record_audit

    record_audit(
        db,
        actor=principal,
        tool_name=tool_name,
        event="verification_succeeded" if verified else "verification_escalated",
        execution_id=execution_id,
        conversation_id=conversation_id,
        detail=(
            f"Read-back verification {'succeeded' if verified else 'failed after retry and re-plan'} "
            f"for {tool_name}; status={verification['status']}."
        ),
        data={
            "verification_status": verification["status"],
            "attempt_count": len(attempts),
            "mismatch_fields": [message.split(" ", 1)[0] for message in mismatches],
            "escalation_delivery_statuses": [
                notification["status"] for notification in escalation
            ],
        },
    )
    execution = db.get(AssistantToolExecution, execution_id)
    if execution is not None:
        execution.result = result
        if not verified:
            execution.error = "Post-write verification mismatch; human review requested."
        db.commit()
    return result
