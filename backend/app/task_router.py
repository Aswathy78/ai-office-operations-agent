"""Task list, manager dashboard, assignment email, calendar feed, and
reassignment / due-date change APIs."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    Employee,
    Meeting,
    Task,
    TaskCalendarSync,
    TaskAuditLog,
    TaskNotification,
    TaskReassignmentRequest,
    TASK_AUDIT_OUTCOME_PENDING_APPROVAL,
    TASK_REASSIGNMENT_STATUS_PENDING,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
)
from app.meeting.schemas import (
    EscalationResponse,
    TeamDashboardResponse,
    TeamDashboardSummaryResponse,
    TeamMemberWorkloadResponse,
    TeamOverdueTaskResponse,
    TaskActorRole,
    TaskAuditLogResponse,
    TaskChangeResponse,
    TaskDueDateUpdateRequest,
    TaskNotificationResponse,
    TaskReassignRequest,
    TaskReassignmentDecisionRequest,
    TaskReassignmentRequestResponse,
    TaskCalendarSyncResponse,
    TaskReminderRunResponse,
    TaskResponse,
    TaskStatusUpdateRequest,
)
from app.task_reassignment_service import (
    TaskActor,
    TaskChangeConflict,
    TaskChangeDenied,
    TaskChangeResult,
    change_task_due_date,
    decide_reassignment_request,
    reassign_task,
    set_task_status,
)
from app.task_calendar_sync_service import (
    InvalidCalendarSyncTransition,
    max_attempts as calendar_sync_max_attempts,
    request_sync,
    retry_failed,
    run_sync_pass,
)
from app.task_reminder_service import FIRST_REMINDER_BEFORE, _as_utc, run_task_reminders
from app.task_service import attempt_assignment_email

router = APIRouter(prefix="/api/v1/tasks", tags=["Tasks"])


def _task_query(db: Session):
    return (
        db.query(Task, Employee, Meeting)
        .join(Employee, Task.assignee_id == Employee.id)
        # Outer join: assistant-created tasks (Story 6.5) have no source meeting.
        .outerjoin(Meeting, Task.meeting_id == Meeting.id)
        .order_by(Task.due_at, Task.created_at)
    )


def _reassignment_response(
    db: Session,
    request: TaskReassignmentRequest,
) -> TaskReassignmentRequestResponse:
    task = db.get(Task, request.task_id)
    from_assignee = db.get(Employee, request.from_assignee_id)
    to_assignee = db.get(Employee, request.to_assignee_id)
    return TaskReassignmentRequestResponse(
        id=request.id,
        task_id=request.task_id,
        task_title=task.title if task else "",
        from_assignee_id=request.from_assignee_id,
        from_assignee_name=from_assignee.name if from_assignee else "",
        to_assignee_id=request.to_assignee_id,
        to_assignee_name=to_assignee.name if to_assignee else "",
        to_assignee_team=to_assignee.team if to_assignee else None,
        requested_by=request.requested_by,
        requester_role=request.requester_role,
        requester_team=request.requester_team,
        reason=request.reason,
        status=request.status,
        decided_by=request.decided_by,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        created_at=request.created_at,
    )


def _calendar_sync_response(sync: TaskCalendarSync | None) -> TaskCalendarSyncResponse | None:
    if sync is None:
        return None
    return TaskCalendarSyncResponse(
        status=sync.status,
        provider=sync.provider,
        pending_reason=sync.pending_reason,
        attempt_count=sync.attempt_count,
        max_attempts=calendar_sync_max_attempts(),
        next_attempt_at=sync.next_attempt_at,
        last_error=sync.last_error,
        last_synced_at=sync.last_synced_at,
        last_polled_at=sync.last_polled_at,
        external_change_detected_at=sync.external_change_detected_at,
        external_change_detail=sync.external_change_detail,
    )


def _task_response(
    task: Task,
    employee: Employee,
    meeting: Meeting | None,
    pending_reassignment: TaskReassignmentRequestResponse | None = None,
    calendar_sync: TaskCalendarSync | None = None,
) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        title=task.title,
        assignee_id=employee.id,
        assignee_name=employee.name,
        creator_id=task.creator_id,
        source=task.source,
        source_action_item_id=task.source_action_item_id,
        meeting_id=task.meeting_id,
        meeting_title=meeting.title if meeting else None,
        transcript_id=task.transcript_id,
        source_segment_index=task.source_segment_index,
        source_timestamp_seconds=task.source_timestamp_seconds,
        due_at=(
            task.due_at.replace(tzinfo=timezone.utc)
            if task.due_at.tzinfo is None
            else task.due_at.astimezone(timezone.utc)
        ),
        priority=task.priority,
        status=task.status,
        completed_at=task.completed_at,
        assignment_email_status=task.assignment_email_status,
        assignment_email_error=task.assignment_email_error,
        created_at=task.created_at,
        assignee_email=employee.email,
        assignee_team=employee.team,
        updated_at=task.updated_at,
        calendar_sequence=task.calendar_sequence or 0,
        pending_reassignment=pending_reassignment,
        calendar_sync=_calendar_sync_response(calendar_sync),
    )


def _task_responses(
    db: Session,
    rows: list[tuple[Task, Employee, Meeting | None]],
) -> list[TaskResponse]:
    task_ids = [task.id for task, _, _ in rows]
    pending: dict[str, TaskReassignmentRequest] = {}
    syncs: dict[str, TaskCalendarSync] = {}
    if task_ids:
        syncs = {
            sync.task_id: sync
            for sync in db.query(TaskCalendarSync).filter(TaskCalendarSync.task_id.in_(task_ids)).all()
        }
    if task_ids:
        pending = {
            request.task_id: request
            for request in db.query(TaskReassignmentRequest)
            .filter(
                TaskReassignmentRequest.task_id.in_(task_ids),
                TaskReassignmentRequest.status == TASK_REASSIGNMENT_STATUS_PENDING,
            )
            .all()
        }
    return [
        _task_response(
            task,
            employee,
            meeting,
            _reassignment_response(db, pending[task.id]) if task.id in pending else None,
            syncs.get(task.id),
        )
        for task, employee, meeting in rows
    ]


def _task_row(db: Session, task_id: str) -> tuple[Task, Employee, Meeting | None] | None:
    return _task_query(db).filter(Task.id == task_id).one_or_none()


@router.get("/", response_model=list[TaskResponse])
def get_assigned_tasks(
    assignee_email: str | None = Query(default=None, min_length=1),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    query = _task_query(db)
    if principal.role in {"manager", "hr_operations", "admin"} and assignee_email:
        query = query.filter(Employee.email == assignee_email)
    else:
        query = query.filter(Task.assignee_id == principal.user_id)
    rows = query.all()
    return _task_responses(db, rows)


@router.get("/dashboard", response_model=list[TaskResponse])
def get_task_dashboard(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    query = _task_query(db)
    if principal.role not in {"manager", "hr_operations", "admin"}:
        query = query.filter(Task.assignee_id == principal.user_id)
    rows = query.all()
    return _task_responses(db, rows)


@router.get("/escalations", response_model=list[EscalationResponse])
def get_task_escalations(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    if principal.role == "employee":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employees cannot access the manager escalation dashboard.",
        )

    query = (
        db.query(TaskNotification, Task, Employee)
        .join(Task, TaskNotification.task_id == Task.id)
        .join(Employee, Task.assignee_id == Employee.id)
        .filter(TaskNotification.kind == "escalation")
        .filter(Task.status == TASK_STATUS_PENDING)
    )

    if principal.role == "manager":
        current_emp = (
            db.query(Employee)
            .filter((Employee.id == principal.user_id) | (Employee.email == principal.email))
            .first()
        )
        manager_ids = {principal.user_id}
        if current_emp:
            manager_ids.add(current_emp.id)
        query = query.filter(Employee.manager_id.in_(list(manager_ids)))
    elif principal.role not in ("hr_operations", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unauthorized to access escalations.",
        )

    results = query.order_by(TaskNotification.created_at.desc()).all()
    now = datetime.now(timezone.utc)
    from app.task_reminder_service import count_missed_reminders, _as_utc

    escalations = []
    seen_tasks = set()
    for notification, task, assignee in results:
        if task.id in seen_tasks:
            continue
        seen_tasks.add(task.id)

        due_utc = _as_utc(task.due_at)
        overdue_hours = max(0, int((now - due_utc).total_seconds() // 3600))
        missed = count_missed_reminders(db, task, now)

        escalations.append(
            EscalationResponse(
                task_id=task.id,
                task_title=task.title,
                assignee_id=assignee.id,
                assignee_name=assignee.name,
                assignee_email=assignee.email,
                due_at=task.due_at,
                overdue_hours=overdue_hours,
                missed_reminders=missed,
                escalation_timestamp=notification.created_at,
                escalation_status=notification.status,
                escalation_notification_id=notification.id,
            )
        )
    return escalations


@router.get("/team-dashboard", response_model=TeamDashboardResponse)
def get_manager_team_dashboard(
    team: str | None = Query(default=None, description="Optional team filter for admin/HR"),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    if principal.role not in ("manager", "hr_operations", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employees cannot access the manager team dashboard.",
        )

    current_emp = (
        db.query(Employee)
        .filter((Employee.id == principal.user_id) | (Employee.email == principal.email))
        .first()
    )
    manager_ids = {principal.user_id}
    if current_emp:
        manager_ids.add(current_emp.id)

    manager_team = current_emp.team if current_emp and current_emp.team else principal.team

    # Team isolation rules:
    # 1. Managers see ONLY their own team. Query parameter is ignored for managers to prevent manipulation.
    # 2. Primary hierarchy boundary: employees where manager_id matches the manager.
    # 3. Fallback for unassigned manager_id: employees in the same team without another manager.
    if principal.role == "manager":
        team_filter_clauses = [Employee.manager_id.in_(list(manager_ids))]
        if manager_team:
            team_filter_clauses.append(
                and_(
                    Employee.manager_id.is_(None),
                    func.lower(Employee.team) == manager_team.casefold(),
                    ~Employee.id.in_(list(manager_ids)),
                )
            )
        team_members = (
            db.query(Employee)
            .filter(
                Employee.is_active.is_(True),
                or_(*team_filter_clauses),
            )
            .order_by(Employee.name.asc())
            .all()
        )
        resolved_team_name = manager_team or "My Team"
    else:
        # HR Operations / Admin
        resolved_team_name = team or manager_team or "Organization"
        emp_query = db.query(Employee).filter(Employee.is_active.is_(True))
        if team:
            emp_query = emp_query.filter(func.lower(Employee.team) == team.casefold())
        elif manager_team:
            emp_query = emp_query.filter(func.lower(Employee.team) == manager_team.casefold())
        team_members = emp_query.order_by(Employee.name.asc()).all()

    now = datetime.now(timezone.utc)
    due_soon_threshold = now + FIRST_REMINDER_BEFORE

    if not team_members:
        return TeamDashboardResponse(
            summary=TeamDashboardSummaryResponse(
                team_name=resolved_team_name,
                total_members=0,
                total_tasks=0,
                open_tasks=0,
                due_soon_tasks=0,
                overdue_tasks=0,
                completed_tasks=0,
                completion_rate=0.0,
            ),
            members=[],
            overdue_tasks=[],
        )

    member_ids = [m.id for m in team_members]

    # Aggregate task counts per team member in a single database query
    stats_query = (
        db.query(
            Task.assignee_id,
            func.count(Task.id).label("total"),
            func.coalesce(
                func.sum(case((Task.status == TASK_STATUS_COMPLETED, 1), else_=0)), 0
            ).label("completed"),
            func.coalesce(
                func.sum(case((Task.status == TASK_STATUS_PENDING, 1), else_=0)), 0
            ).label("open"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Task.status == TASK_STATUS_PENDING,
                                Task.due_at < now,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("overdue"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Task.status == TASK_STATUS_PENDING,
                                Task.due_at >= now,
                                Task.due_at <= due_soon_threshold,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("due_soon"),
        )
        .filter(Task.assignee_id.in_(member_ids))
        .group_by(Task.assignee_id)
        .all()
    )

    stats_by_assignee = {
        row.assignee_id: {
            "total": int(row.total),
            "completed": int(row.completed),
            "open": int(row.open),
            "overdue": int(row.overdue),
            "due_soon": int(row.due_soon),
        }
        for row in stats_query
    }

    # Fetch separate overdue tasks in a single query
    overdue_rows = (
        db.query(Task, Employee)
        .join(Employee, Task.assignee_id == Employee.id)
        .filter(
            Task.assignee_id.in_(member_ids),
            Task.status == TASK_STATUS_PENDING,
            Task.due_at < now,
        )
        .order_by(Task.due_at.asc())
        .all()
    )

    members_response = []
    total_tasks_sum = 0
    open_tasks_sum = 0
    due_soon_tasks_sum = 0
    overdue_tasks_sum = 0
    completed_tasks_sum = 0

    for emp in team_members:
        emp_stats = stats_by_assignee.get(
            emp.id,
            {"total": 0, "completed": 0, "open": 0, "overdue": 0, "due_soon": 0},
        )
        emp_total = emp_stats["total"]
        emp_completed = emp_stats["completed"]
        emp_completion_rate = (
            round((emp_completed / emp_total) * 100, 1) if emp_total > 0 else 0.0
        )

        total_tasks_sum += emp_total
        open_tasks_sum += emp_stats["open"]
        due_soon_tasks_sum += emp_stats["due_soon"]
        overdue_tasks_sum += emp_stats["overdue"]
        completed_tasks_sum += emp_completed

        members_response.append(
            TeamMemberWorkloadResponse(
                employee_id=emp.id,
                employee_name=emp.name,
                employee_email=emp.email,
                total_tasks=emp_total,
                open_tasks=emp_stats["open"],
                due_soon_tasks=emp_stats["due_soon"],
                overdue_tasks=emp_stats["overdue"],
                completed_tasks=emp_completed,
                completion_rate=emp_completion_rate,
            )
        )

    overall_completion_rate = (
        round((completed_tasks_sum / total_tasks_sum) * 100, 1)
        if total_tasks_sum > 0
        else 0.0
    )

    overdue_response = []
    for task, assignee in overdue_rows:
        due_utc = _as_utc(task.due_at)
        overdue_hours = max(0, int((now - due_utc).total_seconds() // 3600))
        overdue_response.append(
            TeamOverdueTaskResponse(
                task_id=task.id,
                title=task.title,
                assignee_id=assignee.id,
                assignee_name=assignee.name,
                assignee_email=assignee.email,
                due_at=due_utc,
                overdue_hours=overdue_hours,
                priority=task.priority,
                status=task.status,
            )
        )

    summary_response = TeamDashboardSummaryResponse(
        team_name=resolved_team_name,
        total_members=len(team_members),
        total_tasks=total_tasks_sum,
        open_tasks=open_tasks_sum,
        due_soon_tasks=due_soon_tasks_sum,
        overdue_tasks=overdue_tasks_sum,
        completed_tasks=completed_tasks_sum,
        completion_rate=overall_completion_rate,
    )

    return TeamDashboardResponse(
        summary=summary_response,
        members=members_response,
        overdue_tasks=overdue_response,
    )


def _ical_stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@router.get("/calendar.ics")
def get_task_calendar(
    assignee_email: str = Query(min_length=1),
    db: Session = Depends(get_db),
):
    rows = _task_query(db).filter(Employee.email == assignee_email).all()
    events = []
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for task, _, meeting in rows:
        source = (
            f"Source meeting: {meeting.title}; "
            f"Transcript timestamp: {task.source_timestamp_seconds:.2f}s"
            if meeting is not None
            else "Source: AI Office Assistant"
        )
        description = _escape_ical_text(f"Priority: {task.priority}; {source}")
        events.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{task.id}@ai-office-operations",
                f"DTSTAMP:{now}",
                # SEQUENCE/LAST-MODIFIED let calendar clients replace the event
                # in place when the due date changes.
                f"SEQUENCE:{task.calendar_sequence or 0}",
                f"LAST-MODIFIED:{_ical_stamp(task.updated_at or task.created_at)}",
                f"DTSTART:{_ical_stamp(task.due_at)}",
                f"SUMMARY:{_escape_ical_text(task.title)}",
                f"DESCRIPTION:{description}",
                "END:VEVENT",
            ]
        )
    calendar = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//AI Office Operations Agent//Tasks//EN",
            "CALSCALE:GREGORIAN",
            *events,
            "END:VCALENDAR",
            "",
        ]
    )
    return Response(
        content=calendar,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tasks.ics"'},
    )


def _escape_ical_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


@router.post(
    "/{task_id}/assignment-email/retry",
    response_model=TaskResponse,
)
def retry_assignment_email(task_id: str, db: Session = Depends(get_db)):
    if _task_row(db, task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    try:
        attempt_assignment_email(db, task_id, retry_failed=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    row = _task_row(db, task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return _task_responses(db, [row])[0]


def _actor(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> TaskActor:
    return TaskActor(
        id=principal.user_id,
        role=principal.role,
        team=principal.team,
        email=principal.email,
    )


def _notification_responses(
    db: Session,
    notifications: list[TaskNotification],
) -> list[TaskNotificationResponse]:
    responses = []
    for notification in notifications:
        recipient = db.get(Employee, notification.recipient_employee_id)
        responses.append(
            TaskNotificationResponse(
                id=notification.id,
                recipient_employee_id=notification.recipient_employee_id,
                recipient_name=recipient.name if recipient else "",
                kind=notification.kind,
                subject=notification.subject,
                status=notification.status,
                error=notification.error,
                created_at=notification.created_at,
            )
        )
    return responses


def _run_change(db: Session, change) -> JSONResponse:
    try:
        result: TaskChangeResult = change()
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TaskChangeDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except TaskChangeConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    row = _task_row(db, result.task.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    body = TaskChangeResponse(
        outcome=result.outcome,
        message=result.message,
        task=_task_responses(db, [row])[0],
        reassignment_request=(
            _reassignment_response(db, result.reassignment_request)
            if result.reassignment_request is not None
            else None
        ),
        notifications=_notification_responses(db, result.notifications or []),
    )
    return JSONResponse(
        status_code=(
            status.HTTP_202_ACCEPTED
            if result.outcome == TASK_AUDIT_OUTCOME_PENDING_APPROVAL
            else status.HTTP_200_OK
        ),
        content=body.model_dump(mode="json"),
    )


@router.patch(
    "/{task_id}/assignee",
    response_model=TaskChangeResponse,
    responses={202: {"model": TaskChangeResponse}},
    summary="Reassign a task (cross-team moves by managers need approval)",
)
def reassign_task_endpoint(
    task_id: str,
    payload: TaskReassignRequest,
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    response = _run_change(
        db,
        lambda: reassign_task(db, task_id, actor, payload.assignee_id, payload.reason),
    )
    _sync_calendar_soon(db, task_id, "Task reassigned")
    return response


@router.patch(
    "/{task_id}/due-date",
    response_model=TaskChangeResponse,
    summary="Change a task's due date, updating its calendar entry",
)
def change_task_due_date_endpoint(
    task_id: str,
    payload: TaskDueDateUpdateRequest,
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    response = _run_change(
        db,
        lambda: change_task_due_date(db, task_id, actor, payload.due_at, payload.reason),
    )
    _sync_calendar_soon(db, task_id, "Due date changed")
    return response


@router.get(
    "/reassignment-requests",
    response_model=list[TaskReassignmentRequestResponse],
)
def list_reassignment_requests(
    request_status: str | None = Query(
        default=TASK_REASSIGNMENT_STATUS_PENDING,
        alias="status",
    ),
    db: Session = Depends(get_db),
):
    query = db.query(TaskReassignmentRequest)
    if request_status:
        query = query.filter(TaskReassignmentRequest.status == request_status)
    requests = query.order_by(TaskReassignmentRequest.created_at.desc()).all()
    return [_reassignment_response(db, request) for request in requests]


@router.post(
    "/reassignment-requests/{request_id}/decision",
    response_model=TaskChangeResponse,
    summary="HR / Operations or Admin approves or rejects a cross-team reassignment",
)
def decide_reassignment_request_endpoint(
    request_id: str,
    payload: TaskReassignmentDecisionRequest,
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    return _run_change(
        db,
        lambda: decide_reassignment_request(
            db, request_id, actor, payload.decision, payload.note
        ),
    )


@router.get("/{task_id}/audit", response_model=list[TaskAuditLogResponse])
def get_task_audit_log(task_id: str, db: Session = Depends(get_db)):
    if db.get(Task, task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    entries = (
        db.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task_id)
        .order_by(TaskAuditLog.created_at.desc(), TaskAuditLog.id)
        .all()
    )
    return [
        TaskAuditLogResponse(
            id=entry.id,
            task_id=entry.task_id,
            actor_id=entry.actor_id,
            actor_role=entry.actor_role,
            action=entry.action,
            outcome=entry.outcome,
            detail=entry.detail,
            created_at=entry.created_at,
        )
        for entry in entries
    ]


@router.get("/{task_id}/notifications", response_model=list[TaskNotificationResponse])
def get_task_notifications(task_id: str, db: Session = Depends(get_db)):
    if db.get(Task, task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    notifications = (
        db.query(TaskNotification)
        .filter(TaskNotification.task_id == task_id)
        .order_by(TaskNotification.created_at.desc())
        .all()
    )
    return _notification_responses(db, notifications)


@router.patch(
    "/{task_id}/status",
    response_model=TaskChangeResponse,
    summary="Mark a task Completed (stops reminders) or reopen it",
)
def update_task_status_endpoint(
    task_id: str,
    payload: TaskStatusUpdateRequest,
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    return _run_change(db, lambda: set_task_status(db, task_id, actor, payload.status))


@router.post(
    "/reminders/run",
    response_model=TaskReminderRunResponse,
    summary="Run the reminder / escalation pass now (the scheduler also runs it automatically)",
)
def run_task_reminders_endpoint(
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    if actor.role not in ("manager", "hr_operations", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only managers, HR / Operations or admins can trigger reminders.",
        )
    return run_task_reminders(db).as_dict()


def _sync_calendar_soon(db: Session, task_id: str, reason: str) -> None:
    """Queue the task's calendar entry and wake the sync scheduler (best effort)."""
    from app.task_calendar_scheduler import wake

    try:
        request_sync(db, task_id, reason)
    except Exception:  # noqa: BLE001 - the scheduler's reconcile pass will catch it up
        db.rollback()
    wake()


def _can_manage_calendar_sync(db: Session, task_id: str, actor: TaskActor) -> bool:
    if actor.role in ("manager", "hr_operations", "admin"):
        return True
    task = db.get(Task, task_id)
    assignee = db.get(Employee, task.assignee_id) if task else None
    return bool(assignee and actor.email and assignee.email.casefold() == actor.email.casefold())


@router.post(
    "/{task_id}/calendar-sync/retry",
    response_model=TaskResponse,
    summary="Retry a failed calendar sync for a task deadline",
)
def retry_task_calendar_sync(
    task_id: str,
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    if _task_row(db, task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if not _can_manage_calendar_sync(db, task_id, actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee or a manager can retry this calendar sync.",
        )
    try:
        retry_failed(db, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidCalendarSyncTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    from app.task_calendar_scheduler import wake

    wake()
    return _task_responses(db, [_task_row(db, task_id)])[0]


@router.post(
    "/calendar-sync/run",
    summary="Run a calendar sync pass now, including the external-change poll",
)
def run_task_calendar_sync(
    actor: TaskActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    if actor.role not in ("manager", "hr_operations", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only managers, HR / Operations or admins can run the calendar sync.",
        )
    return run_sync_pass(db, poll=True).as_dict()
