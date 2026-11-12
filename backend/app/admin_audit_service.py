"""One read-only audit trail for administrators, merged from the records the system already keeps.

Nothing new is written. Tasks, meetings, the AI assistant, e-mail drafts and workflows each
log their own events; this module reads them side by side, newest first, with the person's
name looked up. Raw provider errors are turned into plain language, free text is cut to a
safe length, and the assistant's structured ``data`` payload is never included.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Callable

from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session

from app.meeting.models import (
    AssistantAuditEvent,
    AuthAuditEvent,
    EmailDraftEvent,
    Employee,
    MeetingAuditLog,
    TaskAuditLog,
    WorkflowEvent,
)
from app.plain_errors import plain_error

DETAIL_LIMIT = 300
# Workflows log a "step completed" event every few seconds just by running. It is system
# chatter, not an action anyone took, and it would bury everything that matters.
ROUTINE_WORKFLOW_EVENTS = ("step_completed",)
MAX_LIMIT = 500
SOURCES = ("task", "meeting", "assistant", "email", "workflow", "auth")
SOURCE_LABELS = {
    "task": "Tasks",
    "meeting": "Meetings",
    "assistant": "AI assistant",
    "email": "E-mail",
    "workflow": "Workflows",
    "auth": "Sign-ins",
}


@dataclass(frozen=True)
class _Source:
    key: str
    model: type
    build: Callable[[object], dict]
    search: Callable[[str], object]


ACTION_LABELS = {
    # tasks
    "create": "Task created",
    "reassign": "Task reassigned",
    "change_due_date": "Due date changed",
    "change_status": "Status changed",
    "escalation_triggered": "Escalated to manager",
    "manager_notified": "Manager notified",
    "reminder_scheduled": "Reminder scheduled",
    "reminder_sent": "Reminder sent",
    "reminder_failed": "Reminder failed",
    # meetings
    "meeting_cancelled": "Meeting cancelled",
    "participant_added": "Participant added",
    # AI assistant
    "approval_requested": "Approval requested",
    "approved": "Approved",
    "auto_approved": "Auto-approved",
    "conflict_blocked": "Blocked by a conflict",
    "executed": "Action carried out",
    "permission_denied": "Permission denied",
    # workflows
    "dead_lettered": "Moved to the dead-letter queue",
    "retry_scheduled": "Retry scheduled",
    "lease_expired": "Worker stopped responding",
    "manual_retry": "Retried by an administrator",
    "manual_resume": "Resumed by an administrator",
    "manual_cancel": "Cancelled by an administrator",
    # sign-ins and sessions (Story 10.1)
    "login_success": "Signed in",
    "login_failed": "Sign-in failed",
    "logout": "Signed out",
    "session_revoked": "Session ended by an administrator",
    "session_idle_timeout": "Signed out for inactivity",
    "refresh_reuse_detected": "Session ended: token reuse detected",
}
AUTH_OUTCOMES = {"login_success": "success", "login_failed": "failed", "refresh_reuse_detected": "failed"}
DEFAULT_DETAIL = {
    "manual_retry": "The run was restarted from its first step.",
    "manual_resume": "The run was resumed from its last checkpoint.",
    "manual_cancel": "The run was cancelled and will not run again.",
}
TOOL_LABELS = {
    "task.create": "Create task",
    "email.send": "Send e-mail",
    "meeting.schedule": "Schedule meeting",
}
_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
_TRAILING_ID = re.compile(r"\s*\((?:tool execution|execution|run|draft|approval)\s+[0-9a-f-]{8,}\)", re.IGNORECASE)
# snake_case words (reminder_overdue) read as words; never touches an e-mail address.
_SNAKE = re.compile(r"(?<![\w.@-])[a-z]+(?:_[a-z]+)+(?![\w@])")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")


def _humanize(value: str) -> str:
    text = value.replace("_", " ").replace(".", " ").strip()
    return text[:1].upper() + text[1:]


def _readable_time(match: re.Match) -> str:
    raw = match.group(0)
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment
    return f"{moment.day} {moment.strftime('%b %Y %H:%M')} UTC"


def _tidy(value: str | None) -> str:
    """One readable sentence: no internal tags or ids, times a person can read, a safe length."""
    text = _TAG.sub("", value or "")
    text = _TRAILING_ID.sub("", text)
    text = _ISO_TIME.sub(_readable_time, text)
    text = _SNAKE.sub(lambda match: match.group(0).replace("_", " "), text)
    text = " ".join(text.split())
    return text if len(text) <= DETAIL_LIMIT else text[: DETAIL_LIMIT - 1] + "…"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _entry(source: str, row, *, actor_id: str | None, action: str, outcome: str | None, detail: str,
           actor_role: str | None = None, label: str | None = None, target: str | None = None) -> dict:
    return {
        "id": f"{source}:{row.id}",
        "at": _aware(row.created_at).isoformat(),
        "source": source,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "action": action,
        "action_label": label or ACTION_LABELS.get(action) or _humanize(action),
        # What the action was done to (the tool, the workflow step), when that adds something.
        "target": target,
        "outcome": outcome,
        "detail": _tidy(detail) or DEFAULT_DETAIL.get(action, ""),
    }


def _meeting_detail(details) -> str:
    if not isinstance(details, dict):
        return ""
    parts = []
    for key, value in details.items():
        if not isinstance(value, (str, int, float, bool)) or value == "":
            continue
        if key == "attendee_count" and isinstance(value, int):
            parts.append(f"{value} attendee{'' if value == 1 else 's'}")
        else:
            parts.append(f"{_humanize(key)}: {value}")
    return "; ".join(parts)


def _task(row: TaskAuditLog) -> dict:
    return _entry("task", row, actor_id=row.actor_id, actor_role=row.actor_role,
                  action=row.action, outcome=row.outcome, detail=row.detail)


def _meeting(row: MeetingAuditLog) -> dict:
    return _entry("meeting", row, actor_id=row.actor_id, action=row.action, outcome=None,
                  detail=_meeting_detail(row.details))


def _assistant(row: AssistantAuditEvent) -> dict:
    tool = TOOL_LABELS.get(row.tool_name) or _humanize(row.tool_name)
    return _entry("assistant", row, actor_id=row.actor_id, actor_role=row.actor_role,
                  action=row.event, outcome=row.risk_level, detail=row.detail, target=tool)


def _email(row: EmailDraftEvent) -> dict:
    label = f"{_humanize(row.from_status or 'new')} → {_humanize(row.to_status).lower()}"
    return _entry("email", row, actor_id=row.actor_id, action=f"{row.from_status or 'new'} → {row.to_status}",
                  label=label, outcome=row.to_status, detail=plain_error(row.detail) or "")


def _workflow(row: WorkflowEvent) -> dict:
    return _entry("workflow", row, actor_id=row.actor_id, action=row.event_type, outcome=None,
                  detail=plain_error(row.error) or row.message or "",
                  target=_humanize(row.step_key) if row.step_key else None)


def _auth(row: AuthAuditEvent) -> dict:
    entry = _entry("auth", row, actor_id=row.actor_id or row.user_id, action=row.event,
                   outcome=AUTH_OUTCOMES.get(row.event), detail=row.detail)
    # For an administrator's action, who it was done to is shown as the target.
    entry["_subject_id"] = row.user_id if row.actor_id and row.actor_id != row.user_id else None
    return entry


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_SOURCES: tuple[_Source, ...] = (
    _Source("task", TaskAuditLog, _task,
            lambda q: or_(TaskAuditLog.action.ilike(_like(q), escape="\\"),
                          TaskAuditLog.detail.ilike(_like(q), escape="\\"),
                          TaskAuditLog.outcome.ilike(_like(q), escape="\\"))),
    _Source("meeting", MeetingAuditLog, _meeting,
            lambda q: or_(MeetingAuditLog.action.ilike(_like(q), escape="\\"),
                          cast(MeetingAuditLog.details, String).ilike(_like(q), escape="\\"))),
    _Source("assistant", AssistantAuditEvent, _assistant,
            lambda q: or_(AssistantAuditEvent.event.ilike(_like(q), escape="\\"),
                          AssistantAuditEvent.tool_name.ilike(_like(q), escape="\\"),
                          AssistantAuditEvent.detail.ilike(_like(q), escape="\\"))),
    _Source("email", EmailDraftEvent, _email,
            lambda q: or_(EmailDraftEvent.to_status.ilike(_like(q), escape="\\"),
                          EmailDraftEvent.detail.ilike(_like(q), escape="\\"))),
    _Source("auth", AuthAuditEvent, _auth,
            lambda q: or_(AuthAuditEvent.event.ilike(_like(q), escape="\\"),
                          AuthAuditEvent.provider.ilike(_like(q), escape="\\"),
                          AuthAuditEvent.detail.ilike(_like(q), escape="\\"))),
    _Source("workflow", WorkflowEvent, _workflow,
            lambda q: or_(WorkflowEvent.event_type.ilike(_like(q), escape="\\"),
                          WorkflowEvent.message.ilike(_like(q), escape="\\"),
                          WorkflowEvent.error.ilike(_like(q), escape="\\"))),
)


def list_audit_entries(db: Session, *, source: str | None = None, query: str | None = None,
                       limit: int = 100) -> dict:
    """Newest-first entries, at most ``limit``, plus how many entries each source holds."""
    if source is not None and source not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}.")
    limit = max(1, min(limit, MAX_LIMIT))
    query = (query or "").strip()[:100] or None

    entries: list[dict] = []
    counts: dict[str, int] = {}
    for spec in _SOURCES:
        base = db.query(spec.model)
        if spec.model is WorkflowEvent:
            base = base.filter(WorkflowEvent.event_type.notin_(ROUTINE_WORKFLOW_EVENTS))
        if query:
            base = base.filter(spec.search(query))
        counts[spec.key] = base.count()
        if source in (None, spec.key):
            rows = base.order_by(spec.model.created_at.desc(), spec.model.id.desc()).limit(limit).all()
            entries.extend(spec.build(row) for row in rows)

    entries.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
    entries = entries[:limit]

    ids = {item["actor_id"] for item in entries if item["actor_id"]}
    ids |= {item["_subject_id"] for item in entries if item.get("_subject_id")}
    names = dict(db.query(Employee.id, Employee.name).filter(Employee.id.in_(ids)).all()) if ids else {}
    for item in entries:
        item["actor_name"] = names.get(item["actor_id"]) if item["actor_id"] else None
        subject = item.pop("_subject_id", None)
        if subject and names.get(subject):
            item["target"] = names[subject]
    return {
        "entries": entries,
        "counts": counts,
        "source_labels": SOURCE_LABELS,
        "limit": limit,
    }
