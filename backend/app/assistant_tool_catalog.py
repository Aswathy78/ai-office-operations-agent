"""Built-in tools the AI assistant can select and execute (Story 6.5).

Each handler calls the existing application services, so a tool call has the
same effect as the equivalent action in the UI:

  meeting.schedule              calendar provider create_event + meeting invitations
  meeting.list_upcoming         meeting service
  email.preview                 e-mail drafting, approval rules and sensitive-data scan
  email.send                    e-mail draft workflow (approval or queue -> dispatcher)
  calendar.check_availability   calendar provider free/busy + alternative slots
  task.create                   tasks table, task audit log, assignment e-mail, calendar sync
  task.list                     tasks table
  document.search               company knowledge base (keyword retrieval)
  notification.send_task_notice task notifications + mail provider
  directory.lookup_employees    employee directory and teams
  database.query_records        allow-listed, role-filtered read-only record queries
  workflow.run                  reminder, e-mail dispatch and calendar sync workflows

Write tools use the idempotency key twice: the executor's ledger replays a repeated
call, and the handler derives record-level keys from it (e.g. ``Task.idempotency_key``)
so even a re-run after a failure never creates a second copy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import uuid

from sqlalchemy import func

from app.assistant_tools import (
    NO_RETRY,
    READ_RETRY,
    RetryPolicy,
    ToolConflictError,
    ToolContext,
    ToolExecutionError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolRegistry,
    ToolSpec,
    ToolTransientError,
    max_risk,
    parse_datetime_argument,
)
from app.employee_access import (
    department_visible_employee_ids,
    manager_visible_employee_ids,
)
from app.meeting.calendar_provider import (
    CalendarConflictError,
    CalendarProviderError,
    get_calendar_provider,
)
from app.meeting.models import (
    ACTION_ITEM_PRIORITIES,
    AssistantToolExecution,
    EMAIL_DRAFT_STATUS_APPROVAL,
    EMAIL_DRAFT_STATUS_DRAFT,
    EMAIL_DRAFT_STATUSES,
    EmailDraft,
    Employee,
    Meeting,
    MeetingParticipant,
    TASK_AUDIT_OUTCOME_APPLIED,
    TASK_STATUS_PENDING,
    TASK_STATUSES,
    TOOL_EXECUTION_STATUSES,
    Task,
    TaskAuditLog,
    TaskNotification,
)

ELEVATED_ROLES = frozenset({"hr_operations", "admin"})
_MAX_LIST = 100


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scoped_key(prefix: str, context: ToolContext, max_length: int) -> str:
    """A record-level idempotency key unique to this actor and tool-call key."""
    digest = hashlib.sha256(
        f"{context.principal.user_id}:{context.idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{digest}"[:max_length]


def _employee_summary(employee: Employee) -> dict:
    return {
        "id": employee.id,
        "name": employee.name,
        "email": employee.email,
        "team": employee.team,
        "is_active": employee.is_active,
    }


def _same_team(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def _employee_scope_allowed(db, principal, employee: Employee) -> bool:
    if principal.role in ELEVATED_ROLES:
        return True
    if principal.role == "manager":
        return employee.id in manager_visible_employee_ids(db, principal)
    return employee.id == principal.user_id


def _require_team_scope(context: ToolContext, employee: Employee, verb: str) -> None:
    """Enforce the existing manager-line/team fallback and own-resource rules."""
    principal = context.principal
    if _employee_scope_allowed(context.db, principal, employee):
        return
    raise ToolPermissionDenied(
        f"You are not permitted to {verb} this employee."
    )


def _active_employee_by_email(context: ToolContext, email: str) -> Employee | None:
    return (
        context.db.query(Employee)
        .filter(func.lower(Employee.email) == email.strip().casefold())
        .one_or_none()
    )


def _string_list(max_items: int, item: dict) -> dict:
    return {"type": "array", "items": item, "maxItems": max_items, "uniqueItems": True}


_EMAIL = {"type": "string", "format": "email", "maxLength": 255}
_RECIPIENT_REQUIRED = [
    {"required": ["recipient_emails"], "description": "recipient_emails is given"},
    {"required": ["groups"], "description": "groups is given"},
]


def _window_validator(arguments: dict) -> list[dict]:
    start = parse_datetime_argument(arguments["start_at"])
    end = parse_datetime_argument(arguments["end_at"])
    if end <= start:
        return [{"path": "$.end_at", "message": "must be after start_at"}]
    if end - start > timedelta(hours=24):
        return [{"path": "$.end_at", "message": "the window may be at most 24 hours"}]
    return []


def _future_validator(field: str, label: str):
    def validate(arguments: dict) -> list[dict]:
        if parse_datetime_argument(arguments[field]) <= _now() - timedelta(minutes=1):
            return [{"path": f"$.{field}", "message": f"{label} must be in the future"}]
        return []

    return validate


# ---------------------------------------------------------------------------
# Employee directory
# ---------------------------------------------------------------------------


def lookup_employees(context: ToolContext, arguments: dict) -> dict:
    from app.email_draft_service import _active_employees, _group_members
    from app.meeting.employee_matching import match_employee_name

    db = context.db
    visible_ids = department_visible_employee_ids(db, context.principal)
    directory = [
        employee
        for employee in _active_employees(db)
        if employee.id in visible_ids
    ]
    found: dict[str, dict] = {}
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}

    def add(employee: Employee, matched_by: str) -> None:
        found.setdefault(employee.id, {**_employee_summary(employee), "matched_by": matched_by})

    for name in arguments.get("names", []):
        exact = [e for e in directory if e.name.casefold() == name.strip().casefold()]
        if len(exact) == 1:
            add(exact[0], f"name:{name}")
            continue
        if len(exact) > 1:
            ambiguous[name] = [e.name for e in exact]
            continue
        match = match_employee_name(db, name)
        if match.status == "unique_match" and match.employee_id:
            employee = db.get(Employee, match.employee_id)
            if employee is not None and employee.id in visible_ids:
                add(employee, f"name:{name}")
                continue
        if match.status == "ambiguous_match":
            ambiguous[name] = [
                employee.name
                for employee in directory
                if employee.id in set(match.candidate_employee_ids)
            ]
            if ambiguous[name]:
                continue
            unresolved.append(name)
        else:
            unresolved.append(name)

    for email in arguments.get("emails", []):
        employee = _active_employee_by_email(context, email)
        if employee is not None and employee.is_active:
            add(employee, f"email:{email}")
        else:
            unresolved.append(email)

    groups: list[dict] = []
    for group in arguments.get("groups", []):
        members = _group_members(directory, group)
        if not members:
            unresolved.append(group)
            continue
        groups.append({"name": group, "count": len(members)})
        for member in members:
            add(member, f"group:{group}")

    return {
        "employees": list(found.values()),
        "groups": groups,
        "unresolved": unresolved,
        "ambiguous": ambiguous,
    }


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def check_availability(context: ToolContext, arguments: dict) -> dict:
    from app.meeting.scheduling_service import find_alternative_slots

    start = parse_datetime_argument(arguments["start_at"])
    end = parse_datetime_argument(arguments["end_at"])
    emails = list(dict.fromkeys(e.strip().casefold() for e in arguments["attendee_emails"]))
    try:
        provider = get_calendar_provider()
        availability = provider.check_availability(context.db, emails, start, end)
    except CalendarProviderError as exc:
        raise ToolTransientError(f"The calendar provider is unavailable: {exc}") from exc

    alternatives = []
    if availability.has_conflicts:
        duration = int((end - start).total_seconds() // 60)
        alternatives = [
            {"start_at": _iso(slot_start), "end_at": _iso(slot_end)}
            for slot_start, slot_end in find_alternative_slots(
                context.db, start, duration, emails, provider=provider, limit=3
            )
        ]
    return {
        "available": not availability.has_conflicts,
        "attendee_emails": emails,
        "start_at": _iso(start),
        "end_at": _iso(end),
        "conflicts": [
            {
                "attendee_email": conflict.attendee_email,
                "attendee_name": conflict.attendee_name,
                "conflicting_event_title": conflict.conflicting_event_title,
                "conflicting_start_at": _iso(conflict.conflicting_start_at),
                "conflicting_end_at": _iso(conflict.conflicting_end_at),
            }
            for conflict in availability.conflicts
        ],
        "alternative_slots": alternatives,
    }


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


def schedule_meeting(context: ToolContext, arguments: dict) -> dict:
    from app.meeting.scheduling_service import (
        find_alternative_slots,
        get_organization_timezone,
        send_meeting_invitations,
    )

    db = context.db
    start = parse_datetime_argument(arguments["start_at"])
    end = start + timedelta(minutes=arguments["duration_minutes"])
    emails = list(dict.fromkeys(e.strip().casefold() for e in arguments["attendee_emails"]))
    provider = get_calendar_provider()

    # Resume: a previous attempt with this idempotency key may already have created it.
    meeting_id = context.progress.get("meeting_id")
    event = provider.get_event(db, meeting_id) if meeting_id else None
    if event is None:
        try:
            event = provider.create_event(
                db=db,
                title=arguments["title"].strip(),
                start_at=start,
                end_at=end,
                organizer_id=context.principal.user_id,
                attendee_emails=emails,
                agenda=arguments.get("agenda") or "Scheduled by the AI Office Assistant.",
            )
        except CalendarConflictError as exc:
            slots = find_alternative_slots(
                db, start, arguments["duration_minutes"], emails, provider=provider, limit=3
            )
            suggestions = ", ".join(_iso(slot_start) for slot_start, _ in slots) or "none found"
            raise ToolConflictError(f"{exc} Next free slots: {suggestions}.") from exc
        except CalendarProviderError as exc:
            raise ToolTransientError(f"The calendar provider is unavailable: {exc}") from exc
        context.save_progress(meeting_id=event.id)

    already_invited = set(context.progress.get("invitations_sent", []))
    employees = {
        employee.email.casefold(): employee
        for employee in db.query(Employee).filter(func.lower(Employee.email).in_(emails)).all()
    }
    pending = [email for email in emails if email not in already_invited]
    sent, failures = send_meeting_invitations(
        meeting=event,
        organizer_name=context.principal.name,
        attendees=[employees[email] for email in pending if email in employees],
        timezone_name=str(get_organization_timezone()),
        raw_emails=[email for email in pending if email not in employees],
    )
    invited = sorted(already_invited | {email.casefold() for email in sent})
    context.save_progress(invitations_sent=invited)

    # Read the event back so the result reflects what the calendar actually holds.
    stored = provider.get_event(db, event.id)
    if stored is None:
        raise ToolExecutionError("The meeting could not be read back from the calendar.")
    return {
        "meeting_id": stored.id,
        "title": stored.title,
        "start_at": _iso(stored.start_at),
        "end_at": _iso(stored.end_at),
        "join_url": stored.join_url,
        "attendee_emails": stored.attendee_emails,
        "invitations_sent": invited,
        "invitation_failures": failures,
        "provider": stored.provider_name,
    }


def list_upcoming_meetings(context: ToolContext, arguments: dict) -> dict:
    from app.meeting.service import get_user_meetings

    now = _now()
    meetings = get_user_meetings(
        context.db,
        user_id=context.principal.user_id,
        email=context.principal.email,
        start_at=now,
        end_at=now + timedelta(days=arguments.get("days_ahead", 14)),
    )
    unique = {m.id: m for m in meetings if m.status not in ("cancelled", "completed")}
    ordered = sorted(unique.values(), key=lambda m: m.start_at)[: arguments.get("limit", 20)]
    return {
        "meetings": [
            {
                "id": m.id,
                "title": m.title,
                "start_at": _iso(m.start_at),
                "end_at": _iso(m.end_at),
                "status": m.status,
                "join_url": m.join_url,
                "is_organizer": m.organizer_id == context.principal.user_id,
            }
            for m in ordered
        ]
    }


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------


def _split_recipients(context: ToolContext, emails: list[str]):
    named, raw = [], []
    for email in emails:
        employee = _active_employee_by_email(context, email)
        if employee is not None and employee.is_active:
            named.append(employee)
        else:
            raw.append(email)
    return named, raw


def preview_email(context: ToolContext, arguments: dict) -> dict:
    from app.email_draft_service import (
        build_recipients,
        compose_draft,
        evaluate_approval,
        scan_sensitive,
    )

    named, raw = _split_recipients(context, arguments.get("recipient_emails", []))
    recipients, groups, unresolved = build_recipients(
        context.db,
        groups=arguments.get("groups", []),
        employees=named,
        emails=raw,
        exclude_email=context.principal.email,
    )
    # Instructions (e.g. retrieved document facts, Story 7.3) go before the request
    # line, so the e-mail topic is still read from "about …" alone.
    prompt = f"Send an e-mail about {arguments['topic']}."
    if arguments.get("instructions"):
        prompt = f"{arguments['instructions']}\n\n{prompt}"
    content = compose_draft(
        prompt,
        mode=arguments.get("mode", "template"),
        sender=context.principal.name,
        recipients=recipients,
        groups=groups,
        retrieved_context=arguments.get("document_context") or None,
    )
    approval_required, approval_reasons = evaluate_approval(recipients)
    findings = scan_sensitive(content["subject"], content["body"])
    return {
        "subject": content["subject"],
        "body": content["body"],
        "source": content["source"],
        "generation_note": content["generation_note"],
        "recipients": recipients,
        "recipient_groups": groups,
        "unresolved_recipients": unresolved,
        "approval_required": approval_required,
        "approval_reasons": approval_reasons,
        "sensitive_findings": findings,
    }


_EMAIL_OUTCOMES = {
    EMAIL_DRAFT_STATUS_DRAFT: "needs_review",
    EMAIL_DRAFT_STATUS_APPROVAL: "pending_approval",
    "queued": "queued",
    "sending": "queued",
    "sent": "sent",
    "retry": "retrying",
    "failed": "failed",
}


def send_email(context: ToolContext, arguments: dict) -> dict:
    from app import email_delivery_service as delivery
    from app.email_draft_service import (
        EmailActor,
        EmailDraftConflict,
        EmailDraftDenied,
        create_draft,
        decide_draft,
        send_draft,
        submit_for_approval,
    )

    db = context.db
    principal = context.principal
    actor = EmailActor(id=principal.user_id, role=principal.role, team=principal.team)
    acknowledge = bool(arguments.get("acknowledge_sensitive", False))

    draft_id = context.progress.get("draft_id")
    draft = db.get(EmailDraft, draft_id) if draft_id else None
    if draft is None:
        try:
            draft = create_draft(
                db,
                actor,
                sender_name=principal.name,
                sender_email=principal.email,
                subject=arguments["subject"],
                body=arguments["body"],
                recipient_emails=arguments.get("recipient_emails", []),
                groups=arguments.get("groups", []),
                prompt=arguments.get("request") or f"AI assistant e-mail: {arguments['subject']}",
                generation_note="Prepared and sent by the AI Office Assistant on your confirmation.",
                source_document_ids=arguments.get("document_ids", []),
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        context.save_progress(draft_id=draft.id)

    approver = context.approved_by
    if approver is not None:
        # A high-risk request approved by someone else (Story 6.6): that review
        # covers the sensitive-data warning too.
        acknowledge = True
    try:
        if draft.status == EMAIL_DRAFT_STATUS_DRAFT:
            if draft.approval_required and draft.approval_decision != "approved":
                draft = submit_for_approval(db, draft.id, actor)
                if approver is not None:
                    # Record it in the e-mail approval workflow under the approver's
                    # own identity; decide_draft enforces approver roles.
                    draft = decide_draft(
                        db, draft.id,
                        EmailActor(id=approver["id"], role=approver["role"], team=approver.get("team")),
                        decision="approve",
                        note=f"Approved through assistant approval {approver['approval_id']}.",
                    )
            if draft.status == EMAIL_DRAFT_STATUS_DRAFT and (
                draft.approval_decision == "approved" or not draft.approval_required
            ) and not (draft.sensitive_findings and not acknowledge):
                draft = send_draft(
                    db, draft.id, actor, confirm=True, acknowledge_sensitive=acknowledge
                )
    except EmailDraftConflict as exc:
        raise ToolConflictError(str(exc)) from exc
    except EmailDraftDenied as exc:
        raise ToolPermissionDenied(str(exc)) from exc

    db.refresh(draft)
    return {
        "draft_id": draft.id,
        "status": draft.status,
        "outcome": _EMAIL_OUTCOMES.get(draft.status, draft.status),
        "subject": draft.subject,
        "recipient_count": len(draft.recipients),
        "recipients": [r["email"] for r in draft.recipients],
        "approval_required": draft.approval_required,
        "approval_reasons": draft.approval_reasons,
        "sensitive_labels": sorted({f["label"] for f in draft.sensitive_findings}),
        "events": [
            {"to_status": e.to_status, "detail": e.detail, "created_at": _iso(e.created_at)}
            for e in delivery.list_events(db, draft.id)
        ],
    }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _task_summary(task: Task, assignee: Employee | None, meeting_title: str | None = None) -> dict:
    return {
        "task_id": task.id,
        "title": task.title,
        "assignee": _employee_summary(assignee) if assignee else None,
        "creator_id": task.creator_id,
        "source": task.source,
        "due_at": _iso(task.due_at),
        "priority": task.priority,
        "status": task.status,
        "assignment_email_status": task.assignment_email_status,
        "meeting_id": task.meeting_id,
        "meeting_title": meeting_title,
    }


def create_task(context: ToolContext, arguments: dict) -> dict:
    from app.task_calendar_sync_service import request_sync
    from app.task_service import attempt_assignment_email

    db = context.db
    if "assignee_id" in arguments:
        assignee = db.get(Employee, arguments["assignee_id"])
    else:
        assignee = _active_employee_by_email(context, arguments["assignee_email"])
    if assignee is None:
        raise ToolNotFound("The assignee is not in the employee directory.")
    if not assignee.is_active:
        raise ToolExecutionError(f"{assignee.name} is not an active employee.")
    _require_team_scope(context, assignee, "assign tasks to")

    key = _scoped_key("assistant", context, 100)
    task = db.query(Task).filter(Task.idempotency_key == key).one_or_none()
    if task is None:
        now = _now()
        task = Task(
            id=str(uuid.uuid4()),
            title=arguments["title"].strip(),
            assignee_id=assignee.id,
            creator_id=context.principal.user_id,
            source="assistant",
            idempotency_key=key,
            due_at=parse_datetime_argument(arguments["due_at"]),
            priority=arguments.get("priority", "medium"),
            status=TASK_STATUS_PENDING,
            assignment_email_status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(task)
        db.add(TaskAuditLog(
            id=str(uuid.uuid4()),
            task_id=task.id,
            actor_id=context.principal.user_id,
            actor_role=context.principal.role,
            action="create",
            outcome=TASK_AUDIT_OUTCOME_APPLIED,
            detail=(
                f"Created by the AI assistant for {assignee.name}, due "
                f"{_iso(task.due_at)} (tool execution {context.execution_id})."
            ),
        ))
        db.commit()
    context.save_progress(task_id=task.id)

    if arguments.get("notify_assignee", True):
        task = attempt_assignment_email(db, task.id)

    try:
        from app.task_calendar_scheduler import wake

        request_sync(db, task.id, "Task created by the AI assistant")
        wake()
    except Exception:  # noqa: BLE001 - the scheduler's reconcile pass catches it up
        db.rollback()

    stored = db.get(Task, task.id)
    if stored is None:
        raise ToolExecutionError("The task could not be read back after it was created.")
    return _task_summary(stored, assignee)


def list_tasks(context: ToolContext, arguments: dict) -> dict:
    db = context.db
    principal = context.principal
    query = (
        db.query(Task, Employee, Meeting)
        .join(Employee, Task.assignee_id == Employee.id)
        .outerjoin(Meeting, Task.meeting_id == Meeting.id)
    )
    email = arguments.get("assignee_email")
    if principal.role == "employee":
        if email and email.casefold() != principal.email.casefold():
            raise ToolPermissionDenied("Employees can only list their own tasks.")
        query = query.filter(Employee.id == principal.user_id)
    elif principal.role == "manager":
        if email:
            assignee = _active_employee_by_email(context, email)
            if assignee is None:
                raise ToolNotFound(f"No employee has the e-mail {email}.")
            _require_team_scope(context, assignee, "list tasks of")
            query = query.filter(Employee.id == assignee.id)
        else:
            visible_employee_ids = manager_visible_employee_ids(db, principal)
            query = query.filter(Employee.id.in_(visible_employee_ids))
    elif email:
        query = query.filter(func.lower(Employee.email) == email.casefold())
    if arguments.get("status"):
        query = query.filter(Task.status == arguments["status"])
    rows = query.order_by(Task.due_at, Task.created_at).limit(arguments.get("limit", 20)).all()
    return {
        "tasks": [
            _task_summary(task, employee, meeting.title if meeting else None)
            for task, employee, meeting in rows
        ]
    }


# ---------------------------------------------------------------------------
# Documents / RAG
# ---------------------------------------------------------------------------


def search_documents(context: ToolContext, arguments: dict) -> dict:
    import app.assistant_planner as assistant_planner

    results = assistant_planner.search_company_knowledge(
        arguments["query"],
        limit=arguments.get("limit", 3),
        db=context.db,
        actor_id=context.principal.user_id,
    )
    return {"query": arguments["query"], "results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def send_task_notice(context: ToolContext, arguments: dict) -> dict:
    db = context.db
    task = db.get(Task, arguments["task_id"])
    if task is None:
        raise ToolNotFound("Task not found.")
    assignee = db.get(Employee, task.assignee_id)
    if assignee is None or not assignee.email:
        raise ToolExecutionError("The task's assignee has no e-mail address.")
    if task.creator_id != context.principal.user_id:
        _require_team_scope(context, assignee, "notify")

    key = _scoped_key("assistant-notice", context, 200)
    notification = (
        db.query(TaskNotification).filter(TaskNotification.idempotency_key == key).one_or_none()
    )
    if notification is None:
        subject = arguments.get("subject") or f"Update on your task: {task.title}"
        body = (
            f"Hello {assignee.name},\n\n"
            f"{arguments['message'].strip()}\n\n"
            f"Task: {task.title}\n"
            f"Due: {_iso(task.due_at)}\n\n"
            f"Sent by {context.principal.name} through the AI Office Assistant.\n"
        )
        notification = TaskNotification(
            id=str(uuid.uuid4()),
            task_id=task.id,
            recipient_employee_id=assignee.id,
            kind="assistant_notice",
            subject=subject,
            body=body,
            idempotency_key=key,
            status="pending",
        )
        db.add(notification)
        db.commit()

    from app.notification_service import create_task_notification, deliver_notification

    inbox_item = create_task_notification(
        db,
        notification,
        event_type="task_update",
        summary=notification.subject,
    )
    db.commit()
    if inbox_item.email_status in ("pending", "retrying", "failed"):
        deliver_notification(
            db,
            inbox_item.id,
            force_retry=inbox_item.email_status in ("retrying", "failed"),
        )
    if inbox_item.email_status in ("retrying", "failed"):
        raise ToolTransientError(
            f"The notification could not be delivered: {inbox_item.email_error or 'delivery failed'}"
        )

    return {
        "notification_id": notification.id,
        "task_id": task.id,
        "recipient": _employee_summary(assignee),
        "subject": notification.subject,
        "status": notification.status,
    }


# ---------------------------------------------------------------------------
# Database (read-only, allow-listed entities, role-filtered rows)
# ---------------------------------------------------------------------------


def query_records(context: ToolContext, arguments: dict) -> dict:
    db = context.db
    principal = context.principal
    elevated = principal.role in ELEVATED_ROLES
    entity = arguments["entity"]
    limit = arguments.get("limit", 20)
    status_filter = arguments.get("status")
    since = (
        parse_datetime_argument(arguments["created_after"])
        if arguments.get("created_after")
        else None
    )

    if entity == "tasks":
        query = db.query(Task, Employee).join(Employee, Task.assignee_id == Employee.id)
        if principal.role == "employee":
            query = query.filter(
                (Task.assignee_id == principal.user_id)
                | (Task.creator_id == principal.user_id)
            )
        elif principal.role == "manager":
            visible_employee_ids = manager_visible_employee_ids(db, principal)
            query = query.filter(
                (Task.assignee_id.in_(visible_employee_ids))
                | (Task.creator_id == principal.user_id)
            )
        if status_filter:
            query = query.filter(Task.status == status_filter)
        if since:
            query = query.filter(Task.created_at >= since)
        rows = query.order_by(Task.created_at.desc()).limit(limit).all()
        records = [_task_summary(task, employee) for task, employee in rows]
    elif entity == "meetings":
        query = db.query(Meeting)
        if not elevated:
            participant_ids = db.query(MeetingParticipant.meeting_id).filter(
                (MeetingParticipant.user_id == principal.user_id)
                | (func.lower(MeetingParticipant.email) == principal.email.casefold())
            )
            query = query.filter(
                (Meeting.organizer_id == principal.user_id) | Meeting.id.in_(participant_ids)
            )
        if status_filter:
            query = query.filter(Meeting.status == status_filter)
        if since:
            query = query.filter(Meeting.start_at >= since.replace(tzinfo=None))
        records = [
            {
                "id": m.id,
                "title": m.title,
                "organizer_id": m.organizer_id,
                "start_at": _iso(m.start_at),
                "end_at": _iso(m.end_at),
                "status": m.status,
            }
            for m in query.order_by(Meeting.start_at.desc()).limit(limit).all()
        ]
    elif entity == "email_drafts":
        query = db.query(EmailDraft).filter(EmailDraft.discarded_at.is_(None))
        if not elevated:
            query = query.filter(EmailDraft.created_by_id == principal.user_id)
        if status_filter:
            query = query.filter(EmailDraft.status == status_filter)
        if since:
            query = query.filter(EmailDraft.created_at >= since)
        records = [
            {
                "id": d.id,
                "subject": d.subject,
                "status": d.status,
                "created_by_id": d.created_by_id,
                "recipient_count": len(d.recipients),
                "approval_required": d.approval_required,
                "created_at": _iso(d.created_at),
            }
            for d in query.order_by(EmailDraft.created_at.desc()).limit(limit).all()
        ]
    else:  # tool_executions
        query = db.query(AssistantToolExecution)
        if not elevated:
            query = query.filter(AssistantToolExecution.actor_id == principal.user_id)
        if status_filter:
            query = query.filter(AssistantToolExecution.status == status_filter)
        if since:
            query = query.filter(AssistantToolExecution.created_at >= since)
        records = [
            {
                "id": e.id,
                "tool_name": e.tool_name,
                "actor_id": e.actor_id,
                "status": e.status,
                "risk_tier": e.risk_tier,
                "attempts": e.attempts,
                "created_at": _iso(e.created_at),
            }
            for e in query.order_by(AssistantToolExecution.created_at.desc()).limit(limit).all()
        ]
    return {"entity": entity, "count": len(records), "records": records}


def _query_records_validator(arguments: dict) -> list[dict]:
    allowed = {
        "tasks": TASK_STATUSES,
        "meetings": ("scheduled", "active", "instant", "completed", "cancelled"),
        "email_drafts": EMAIL_DRAFT_STATUSES,
        "tool_executions": TOOL_EXECUTION_STATUSES,
    }[arguments["entity"]]
    status_filter = arguments.get("status")
    if status_filter and status_filter not in allowed:
        return [{
            "path": "$.status",
            "message": f"must be one of {', '.join(allowed)} for {arguments['entity']}",
        }]
    return []


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------


def run_workflow(context: ToolContext, arguments: dict) -> dict:
    workflow = arguments["workflow"]
    if workflow == "task_reminders":
        from app.task_reminder_service import run_task_reminders

        summary = run_task_reminders(context.db).as_dict()
    elif workflow == "email_dispatch":
        from app.email_delivery_service import process_due_emails

        summary = process_due_emails(context.db).as_dict()
    else:
        from app.task_calendar_sync_service import run_sync_pass

        summary = run_sync_pass(context.db, poll=True).as_dict()
    return {"workflow": workflow, "summary": summary, "ran_at": _iso(_now())}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        name="directory.lookup_employees",
        category="employee_directory",
        description="Resolve names, e-mail addresses or teams to active employees.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "names": _string_list(50, {"type": "string", "minLength": 1, "maxLength": 255}),
                "emails": _string_list(50, _EMAIL),
                "groups": _string_list(10, {"type": "string", "minLength": 1, "maxLength": 100}),
            },
            "anyOf": [
                {"required": ["names"], "description": "names is given"},
                {"required": ["emails"], "description": "emails is given"},
                {"required": ["groups"], "description": "groups is given"},
            ],
        },
        required_scopes=frozenset({"directory.read"}),
        **_GUARD_HOOKS["directory.lookup_employees"],
        timeout_seconds=5,
        retry_policy=READ_RETRY,
        handler=lookup_employees,
    ))
    registry.register(ToolSpec(
        name="calendar.check_availability",
        category="calendar",
        description="Check attendees' calendars for conflicts and suggest free slots.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["attendee_emails", "start_at", "end_at"],
            "properties": {
                "attendee_emails": {**_string_list(50, _EMAIL), "minItems": 1},
                "start_at": {"type": "string", "format": "date-time"},
                "end_at": {"type": "string", "format": "date-time"},
            },
        },
        required_scopes=frozenset({"calendar.read"}),
        risk_tier="read",
        timeout_seconds=15,
        retry_policy=READ_RETRY,
        handler=check_availability,
        validator=_window_validator,
    ))
    registry.register(ToolSpec(
        name="meeting.schedule",
        category="meeting",
        description="Create a meeting in the calendar and e-mail invitations to attendees.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "start_at", "duration_minutes", "attendee_emails"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "start_at": {"type": "string", "format": "date-time"},
                "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "attendee_emails": {**_string_list(100, _EMAIL), "minItems": 1},
                "agenda": {"type": "string", "maxLength": 5000},
            },
        },
        required_scopes=frozenset({"meetings.write"}),
        **_GUARD_HOOKS["meeting.schedule"],
        timeout_seconds=30,
        # Invitations are e-mail; never resend them automatically.
        retry_policy=NO_RETRY,
        handler=schedule_meeting,
        validator=_future_validator("start_at", "the meeting start"),
    ))
    registry.register(ToolSpec(
        name="meeting.list_upcoming",
        category="meeting",
        description="List the caller's upcoming meetings.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "days_ahead": {"type": "integer", "minimum": 1, "maximum": 90},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
        required_scopes=frozenset({"meetings.read"}),
        risk_tier="read",
        timeout_seconds=5,
        retry_policy=READ_RETRY,
        handler=list_upcoming_meetings,
    ))
    recipients = {
        "recipient_emails": _string_list(100, _EMAIL),
        "groups": _string_list(10, {"type": "string", "minLength": 1, "maxLength": 100}),
    }
    registry.register(ToolSpec(
        name="email.preview",
        category="email",
        description=(
            "Compose an e-mail and report recipients, approval requirement and "
            "sensitive-data findings without saving or sending anything."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["topic"],
            "properties": {
                **recipients,
                "topic": {"type": "string", "minLength": 1, "maxLength": 500},
                "instructions": {"type": "string", "maxLength": 2000},
                # Story 7.3: retrieved company-document facts for the body.
                "document_context": {"type": "string", "maxLength": 10000},
                "mode": {"type": "string", "enum": ["template", "auto"]},
            },
            "anyOf": _RECIPIENT_REQUIRED,
        },
        required_scopes=frozenset({"email.compose"}),
        risk_tier="read",
        timeout_seconds=40,
        retry_policy=READ_RETRY,
        handler=preview_email,
    ))
    registry.register(ToolSpec(
        name="email.send",
        category="email",
        description=(
            "Create an e-mail draft and send it through the e-mail workflow: queued for "
            "delivery, or submitted for approval when company policy requires it."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["subject", "body"],
            "properties": {
                **recipients,
                "subject": {"type": "string", "minLength": 1, "maxLength": 255},
                "body": {"type": "string", "minLength": 1, "maxLength": 20000},
                "acknowledge_sensitive": {"type": "boolean"},
                # Story 7.4: company documents whose facts the e-mail quotes.
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 36},
                    "maxItems": 8,
                    "uniqueItems": True,
                },
                "request": {"type": "string", "maxLength": 4000},
            },
            "anyOf": _RECIPIENT_REQUIRED,
        },
        required_scopes=frozenset({"email.send"}),
        **_GUARD_HOOKS["email.send"],
        timeout_seconds=30,
        retry_policy=NO_RETRY,
        handler=send_email,
    ))
    registry.register(ToolSpec(
        name="task.create",
        category="task",
        description="Create a task for an employee, record it and e-mail the assignee.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "due_at"],
            "properties": {
                "assignee_email": _EMAIL,
                "assignee_id": {"type": "string", "minLength": 1, "maxLength": 36},
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "due_at": {"type": "string", "format": "date-time"},
                "priority": {"type": "string", "enum": list(ACTION_ITEM_PRIORITIES)},
                "notify_assignee": {"type": "boolean"},
            },
            "anyOf": [
                {"required": ["assignee_email"], "description": "assignee_email is given"},
                {"required": ["assignee_id"], "description": "assignee_id is given"},
            ],
        },
        required_scopes=frozenset({"tasks.write"}),
        **_GUARD_HOOKS["task.create"],
        timeout_seconds=30,
        retry_policy=NO_RETRY,
        handler=create_task,
        validator=_future_validator("due_at", "the due date"),
    ))
    registry.register(ToolSpec(
        name="task.list",
        category="task",
        description="List tasks within the caller's own, reporting-line/team-fallback, or HR/admin scope.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "assignee_email": _EMAIL,
                "status": {"type": "string", "enum": list(TASK_STATUSES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST},
            },
        },
        required_scopes=frozenset({"tasks.read"}),
        **_GUARD_HOOKS["task.list"],
        timeout_seconds=5,
        retry_policy=READ_RETRY,
        handler=list_tasks,
    ))
    registry.register(ToolSpec(
        name="document.search",
        category="document",
        description="Search the company policy knowledge base (keyword retrieval).",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 3, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
        },
        required_scopes=frozenset({"documents.read"}),
        risk_tier="read",
        timeout_seconds=30,
        retry_policy=READ_RETRY,
        handler=search_documents,
    ))
    registry.register(ToolSpec(
        name="notification.send_task_notice",
        category="notification",
        description="E-mail a short notice about a task to its assignee.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "message"],
            "properties": {
                "task_id": {"type": "string", "minLength": 1, "maxLength": 36},
                "message": {"type": "string", "minLength": 1, "maxLength": 2000},
                "subject": {"type": "string", "minLength": 1, "maxLength": 255},
            },
        },
        required_scopes=frozenset({"notifications.send"}),
        **_GUARD_HOOKS["notification.send_task_notice"],
        timeout_seconds=20,
        # Safe to retry: each attempt reuses the same notification row.
        retry_policy=RetryPolicy(
            max_attempts=3, backoff_seconds=(1.0, 5.0), retry_on=(ToolTransientError,)
        ),
        handler=send_task_notice,
    ))
    registry.register(ToolSpec(
        name="database.query_records",
        category="database",
        description=(
            "Read recent tasks, meetings, e-mail drafts or tool executions the caller "
            "may see. Read-only; no free-form SQL."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["entity"],
            "properties": {
                "entity": {
                    "type": "string",
                    "enum": ["tasks", "meetings", "email_drafts", "tool_executions"],
                },
                "status": {"type": "string", "minLength": 1, "maxLength": 30},
                "created_after": {"type": "string", "format": "date-time"},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST},
            },
        },
        required_scopes=frozenset({"database.read"}),
        risk_tier="read",
        timeout_seconds=10,
        retry_policy=READ_RETRY,
        handler=query_records,
        validator=_query_records_validator,
    ))
    registry.register(ToolSpec(
        name="workflow.run",
        category="workflow",
        description=(
            "Run one pass of a background workflow now: task reminders, e-mail "
            "dispatch, or task calendar sync."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["workflow"],
            "properties": {
                "workflow": {
                    "type": "string",
                    "enum": ["task_reminders", "email_dispatch", "task_calendar_sync"],
                },
            },
        },
        required_scopes=frozenset({"workflow.run"}),
        **_GUARD_HOOKS["workflow.run"],
        timeout_seconds=120,
        retry_policy=NO_RETRY,
        handler=run_workflow,
    ))



# ---------------------------------------------------------------------------
# Story 6.6 write guard: permission rules, conflicts, risk and call summaries
# ---------------------------------------------------------------------------

LARGE_MEETING = 5
VERY_LARGE_MEETING = 20
LONG_MEETING_MINUTES = 120
DUPLICATE_EMAIL_WINDOW = timedelta(minutes=15)
DUPLICATE_NOTICE_WINDOW = timedelta(hours=1)


def _assignee(db, arguments: dict) -> Employee | None:
    if "assignee_id" in arguments:
        return db.get(Employee, arguments["assignee_id"])
    return (
        db.query(Employee)
        .filter(func.lower(Employee.email) == arguments["assignee_email"].strip().casefold())
        .one_or_none()
    )


def _team_denial(db, principal, employee: Employee, verb: str) -> list[str]:
    if principal.role in ELEVATED_ROLES:
        return []
    if _employee_scope_allowed(db, principal, employee):
        return []
    if principal.role == "manager":
        return [
            f"Managers can only {verb} employees in their reporting line or unassigned employees on their own team."
        ]
    return [
        f"You are not permitted to {verb} this employee."
    ]


def _directory_emails(db) -> set[str]:
    return {
        email.casefold()
        for (email,) in db.query(Employee.email).filter(Employee.is_active.is_(True)).all()
    }


# -- task.create --------------------------------------------------------------


def authorize_task_create(db, principal, arguments: dict) -> list[str]:
    assignee = _assignee(db, arguments)
    return _team_denial(db, principal, assignee, "assign tasks to") if assignee else []


def authorize_directory_lookup(db, principal, arguments: dict) -> list[str]:
    visible_ids = department_visible_employee_ids(db, principal)
    emails = {email.strip().casefold() for email in arguments.get("emails", [])}
    for email in emails:
        employee = (
            db.query(Employee)
            .filter(func.lower(Employee.email) == email, Employee.is_active.is_(True))
            .one_or_none()
        )
        if employee is not None and employee.id not in visible_ids:
            return ["The employee is outside your department and reporting-line scope."]

    from app.meeting.employee_matching import match_employee_name

    for name in arguments.get("names", []):
        matches = (
            db.query(Employee.id)
            .filter(
                func.lower(func.trim(Employee.name)) == name.strip().casefold(),
                Employee.is_active.is_(True),
            )
            .all()
        )
        if any(employee_id not in visible_ids for (employee_id,) in matches):
            return ["The employee is outside your department and reporting-line scope."]
        if not matches:
            match = match_employee_name(db, name)
            if any(
                employee_id not in visible_ids
                for employee_id in match.candidate_employee_ids
            ):
                return ["The employee is outside your department and reporting-line scope."]
    return []


def authorize_task_list(db, principal, arguments: dict) -> list[str]:
    email = arguments.get("assignee_email")
    if not email or principal.role in ELEVATED_ROLES:
        return []
    employee = (
        db.query(Employee)
        .filter(
            func.lower(Employee.email) == email.strip().casefold(),
            Employee.is_active.is_(True),
        )
        .one_or_none()
    )
    if principal.role == "employee":
        return (
            []
            if employee is not None and employee.id == principal.user_id
            else ["Employees can only list their own tasks."]
        )
    if employee is not None and _employee_scope_allowed(db, principal, employee):
        return []
    return ["You are not permitted to list tasks for this employee."]


def conflicts_task_create(db, principal, arguments: dict) -> list[dict]:
    assignee = _assignee(db, arguments)
    if assignee is None:
        return [{"code": "unknown_assignee", "message": "The assignee is not in the employee directory."}]
    if not assignee.is_active:
        return [{"code": "inactive_assignee", "message": f"{assignee.name} is not an active employee."}]
    duplicate = (
        db.query(Task)
        .filter(
            Task.assignee_id == assignee.id,
            Task.status == TASK_STATUS_PENDING,
            func.lower(Task.title) == arguments["title"].strip().casefold(),
        )
        .first()
    )
    if duplicate is not None:
        return [{
            "code": "duplicate_task",
            "message": (
                f"{assignee.name} already has an open task \"{duplicate.title}\" "
                f"due {_iso(duplicate.due_at)}."
            ),
        }]
    return []


def risk_task_create(db, principal, arguments: dict) -> tuple[str, list[str]]:
    level, reasons = "low", []
    assignee = _assignee(db, arguments)
    if arguments.get("priority") in ("high", "urgent"):
        level = "medium"
        reasons.append(f"priority is {arguments['priority']}")
    if parse_datetime_argument(arguments["due_at"]) - _now() < timedelta(hours=24):
        level = "medium"
        reasons.append("due within 24 hours")
    actor = db.get(Employee, principal.user_id)
    actor_team = actor.team if actor is not None else principal.team
    if assignee is not None and not _same_team(assignee.team, actor_team):
        level = "high"
        reasons.append(f"{assignee.name} is outside your team ({assignee.team or 'no team'})")
    return level, reasons


def describe_task_create(arguments: dict) -> str:
    who = arguments.get("assignee_email") or arguments.get("assignee_id")
    return (
        f"Create task \"{arguments['title']}\" for {who}, due {arguments['due_at']} "
        f"({arguments.get('priority', 'medium')} priority)"
    )


# -- meeting.schedule -----------------------------------------------------------


def _meeting_window(arguments: dict) -> tuple[datetime, datetime, list[str]]:
    start = parse_datetime_argument(arguments["start_at"])
    end = start + timedelta(minutes=arguments["duration_minutes"])
    emails = list(dict.fromkeys(e.strip().casefold() for e in arguments["attendee_emails"]))
    return start, end, emails


def conflicts_meeting_schedule(db, principal, arguments: dict) -> list[dict]:
    from app.meeting.scheduling_service import find_alternative_slots

    start, end, emails = _meeting_window(arguments)
    people = list(dict.fromkeys(emails + [principal.email.casefold()]))
    try:
        provider = get_calendar_provider()
        availability = provider.check_availability(db, people, start, end)
    except CalendarProviderError as exc:
        return [{"code": "calendar_unavailable", "message": f"The calendar is unavailable: {exc}"}]
    if not availability.has_conflicts:
        return []
    slots = find_alternative_slots(
        db, start, arguments["duration_minutes"], people, provider=provider, limit=3
    )
    suggestion = (
        " Free slots: " + ", ".join(_iso(slot_start) for slot_start, _ in slots) + "."
        if slots else ""
    )
    return [
        {
            "code": "calendar_conflict",
            "message": (
                f"{c.attendee_name or c.attendee_email} is busy with "
                f"'{c.conflicting_event_title}' from {_iso(c.conflicting_start_at)} "
                f"to {_iso(c.conflicting_end_at)}." + suggestion
            ),
        }
        for c in availability.conflicts
    ]


def risk_meeting_schedule(db, principal, arguments: dict) -> tuple[str, list[str]]:
    from app.meeting.scheduling_service import (
        _fits_attendee_working_hours,
        get_organization_timezone,
    )

    start, end, emails = _meeting_window(arguments)
    directory = _directory_emails(db)
    external = [email for email in emails if email not in directory]
    level, reasons = "low", []
    if external:
        level = "high"
        reasons.append("external attendee(s): " + ", ".join(external))
    if len(emails) > VERY_LARGE_MEETING:
        level = "high"
        reasons.append(f"{len(emails)} attendees (more than {VERY_LARGE_MEETING})")
    elif len(emails) > LARGE_MEETING:
        level = max_risk(level, "medium")
        reasons.append(f"{len(emails)} attendees (more than {LARGE_MEETING})")
    if arguments["duration_minutes"] > LONG_MEETING_MINUTES:
        level = max_risk(level, "medium")
        reasons.append(f"longer than {LONG_MEETING_MINUTES} minutes")
    employees = {
        employee.email.casefold(): employee
        for employee in db.query(Employee).filter(func.lower(Employee.email).in_(emails)).all()
    }
    if employees and not _fits_attendee_working_hours(
        start, end, list(employees), employees, get_organization_timezone()
    ):
        level = max_risk(level, "medium")
        reasons.append("outside the working hours of at least one attendee")
    return level, reasons


def describe_meeting_schedule(arguments: dict) -> str:
    return (
        f"Schedule \"{arguments['title']}\" at {arguments['start_at']} for "
        f"{arguments['duration_minutes']} min with {', '.join(arguments['attendee_emails'])}"
    )


# -- email.send ------------------------------------------------------------------


def _email_recipients(db, principal, arguments: dict):
    from app.email_draft_service import _active_employees, build_recipients

    by_email = {e.email.casefold(): e for e in _active_employees(db)}
    emails = arguments.get("recipient_emails", [])
    return build_recipients(
        db,
        groups=arguments.get("groups", []),
        employees=[by_email[e.casefold()] for e in emails if e.casefold() in by_email],
        emails=[e for e in emails if e.casefold() not in by_email],
        exclude_email=principal.email,
    )


def authorize_email_send(db, principal, arguments: dict) -> list[str]:
    from app.email_draft_service import EVERYONE_GROUP

    everyone = any(g.casefold() == EVERYONE_GROUP.casefold() for g in arguments.get("groups", []))
    if everyone and principal.role == "employee":
        return ["Only managers, HR / Operations or admins can e-mail everyone through the assistant."]
    # Story 7.4: re-checked whenever the e-mail actually runs (chat confirm, the
    # Approvals page, the tools API), so a revoked grant stops it. Never named.
    from app.documents.service import accessible_document_versions

    quoted = set(arguments.get("document_ids", []))
    if quoted - set(accessible_document_versions(db, principal.user_id, quoted)):
        return [
            "This e-mail quotes company documents you no longer have access to, "
            "so it cannot be sent."
        ]
    return []


def _recipients_without_document_access(db, recipients: list[dict], document_ids: list[str]) -> list[str]:
    """Recipients who may not read every quoted document (Story 7.4)."""
    from app.documents.service import accessible_document_versions

    wanted = set(document_ids)
    by_email = {
        employee.email.casefold(): employee.id
        for employee in db.query(Employee).filter(Employee.is_active.is_(True)).all()
    }
    outside = []
    for recipient in recipients:
        employee_id = by_email.get(recipient["email"].casefold())
        if employee_id is None or wanted - set(accessible_document_versions(db, employee_id, wanted)):
            outside.append(recipient["email"])
    return outside


def conflicts_email_send(db, principal, arguments: dict) -> list[dict]:
    recipients, _, unresolved = _email_recipients(db, principal, arguments)
    if unresolved:
        return [{
            "code": "unresolved_recipients",
            "message": "Not a valid e-mail address or directory group: " + ", ".join(unresolved) + ".",
        }]
    if not recipients:
        return [{"code": "no_recipients", "message": "The e-mail has no recipients."}]
    wanted = sorted(r["email"].casefold() for r in recipients)
    recent = (
        db.query(EmailDraft)
        .filter(
            EmailDraft.created_by_id == principal.user_id,
            EmailDraft.discarded_at.is_(None),
            EmailDraft.status != EMAIL_DRAFT_STATUS_DRAFT,
            EmailDraft.created_at >= _now() - DUPLICATE_EMAIL_WINDOW,
            func.lower(EmailDraft.subject) == arguments["subject"].strip().casefold(),
        )
        .all()
    )
    for draft in recent:
        if sorted(r["email"].casefold() for r in draft.recipients) == wanted:
            return [{
                "code": "duplicate_email",
                "message": (
                    f"You already sent \"{draft.subject}\" to the same recipients "
                    f"in the last {int(DUPLICATE_EMAIL_WINDOW.total_seconds() // 60)} minutes "
                    f"(it is {draft.status})."
                ),
            }]
    return []


def risk_email_send(db, principal, arguments: dict) -> tuple[str, list[str]]:
    from app.email_draft_service import approval_recipient_threshold, scan_sensitive

    recipients, _, _ = _email_recipients(db, principal, arguments)
    level, reasons = "medium", ["an e-mail cannot be recalled once delivered"]
    external = [r["email"] for r in recipients if not r["internal"]]
    if external:
        level = "high"
        reasons.append("external recipient(s): " + ", ".join(external))
    threshold = approval_recipient_threshold()
    if len(recipients) > threshold:
        level = "high"
        reasons.append(f"{len(recipients)} recipients (more than {threshold})")
    findings = scan_sensitive(arguments["subject"], arguments["body"])
    if findings:
        level = "high"
        reasons.append("possible sensitive data: " + ", ".join(sorted({f["label"] for f in findings})))
    if arguments.get("document_ids"):
        outside = _recipients_without_document_access(db, recipients, arguments["document_ids"])
        if outside:
            level = "high"
            reasons.append(
                "quotes a restricted company document to recipient(s) outside its "
                "access list: " + ", ".join(outside)
            )
    return level, reasons


def describe_email_send(arguments: dict) -> str:
    to = arguments.get("recipient_emails", []) + [f"{g} team" for g in arguments.get("groups", [])]
    return f"Send e-mail \"{arguments['subject']}\" to {', '.join(to)}"


# -- notification.send_task_notice ----------------------------------------------


def authorize_task_notice(db, principal, arguments: dict) -> list[str]:
    task = db.get(Task, arguments["task_id"])
    if task is None or task.creator_id == principal.user_id:
        return []
    assignee = db.get(Employee, task.assignee_id)
    return _team_denial(db, principal, assignee, "notify") if assignee else []


def conflicts_task_notice(db, principal, arguments: dict) -> list[dict]:
    task = db.get(Task, arguments["task_id"])
    if task is None:
        return [{"code": "unknown_task", "message": "Task not found."}]
    if task.status != TASK_STATUS_PENDING:
        return [{"code": "task_closed", "message": f"The task \"{task.title}\" is already {task.status}."}]
    message = arguments["message"].strip()
    recent = (
        db.query(TaskNotification)
        .filter(
            TaskNotification.task_id == task.id,
            TaskNotification.kind == "assistant_notice",
            TaskNotification.status == "sent",
            TaskNotification.created_at >= _now() - DUPLICATE_NOTICE_WINDOW,
        )
        .all()
    )
    if any(message in notice.body for notice in recent):
        return [{"code": "duplicate_notice", "message": "The same notice was sent for this task in the last hour."}]
    return []


def risk_task_notice(db, principal, arguments: dict) -> tuple[str, list[str]]:
    from app.email_draft_service import scan_sensitive

    findings = scan_sensitive(arguments.get("subject", ""), arguments["message"])
    if findings:
        return "high", ["possible sensitive data: " + ", ".join(sorted({f["label"] for f in findings}))]
    return "low", []


def describe_task_notice(arguments: dict) -> str:
    return f"Send a notice about task {arguments['task_id']}: \"{arguments['message'][:80]}\""


# -- workflow.run ------------------------------------------------------------------


def authorize_workflow_run(db, principal, arguments: dict) -> list[str]:
    if principal.role not in ELEVATED_ROLES:
        return ["This workflow operates across the organization and is limited to HR / Operations or admins."]
    return []


def conflicts_workflow_run(db, principal, arguments: dict) -> list[dict]:
    from app.assistant_tools import arguments_hash

    running = (
        db.query(AssistantToolExecution)
        .filter(
            AssistantToolExecution.tool_name == "workflow.run",
            AssistantToolExecution.arguments_hash == arguments_hash(arguments),
            AssistantToolExecution.status == "running",
        )
        .first()
    )
    if running is not None:
        return [{"code": "workflow_running", "message": f"The {arguments['workflow']} workflow is already running."}]
    return []


def risk_workflow_run(db, principal, arguments: dict) -> tuple[str, list[str]]:
    return "high", ["runs for every employee, not just you"]


def describe_workflow_run(arguments: dict) -> str:
    return f"Run the {arguments['workflow'].replace('_', ' ')} workflow now"


_GUARD_HOOKS = {
    "directory.lookup_employees": dict(
        risk_tier="read", authorize=authorize_directory_lookup,
    ),
    "task.create": dict(
        risk_tier="low", authorize=authorize_task_create,
        detect_conflicts=conflicts_task_create, assess_risk=risk_task_create,
        describe_call=describe_task_create,
    ),
    "meeting.schedule": dict(
        risk_tier="low", detect_conflicts=conflicts_meeting_schedule,
        assess_risk=risk_meeting_schedule, describe_call=describe_meeting_schedule,
    ),
    "email.send": dict(
        risk_tier="medium", authorize=authorize_email_send,
        detect_conflicts=conflicts_email_send, assess_risk=risk_email_send,
        describe_call=describe_email_send,
    ),
    "notification.send_task_notice": dict(
        risk_tier="low", authorize=authorize_task_notice,
        detect_conflicts=conflicts_task_notice, assess_risk=risk_task_notice,
        describe_call=describe_task_notice,
    ),
    "workflow.run": dict(
        risk_tier="high", authorize=authorize_workflow_run,
        detect_conflicts=conflicts_workflow_run, assess_risk=risk_workflow_run,
        describe_call=describe_workflow_run,
    ),
    "task.list": dict(risk_tier="read", authorize=authorize_task_list),
}
