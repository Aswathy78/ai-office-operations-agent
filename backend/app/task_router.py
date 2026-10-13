"""Task list, manager dashboard, assignment email, and calendar feed APIs."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.meeting.models import Employee, Meeting, Task
from app.meeting.schemas import TaskResponse
from app.task_service import attempt_assignment_email

router = APIRouter(prefix="/api/v1/tasks", tags=["Tasks"])


def _task_query(db: Session):
    return (
        db.query(Task, Employee, Meeting)
        .join(Employee, Task.assignee_id == Employee.id)
        .join(Meeting, Task.meeting_id == Meeting.id)
        .order_by(Task.due_at, Task.created_at)
    )


def _task_response(task: Task, employee: Employee, meeting: Meeting) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        title=task.title,
        assignee_id=employee.id,
        assignee_name=employee.name,
        creator_id=task.creator_id,
        source=task.source,
        source_action_item_id=task.source_action_item_id,
        meeting_id=task.meeting_id,
        meeting_title=meeting.title,
        transcript_id=task.transcript_id,
        source_segment_index=task.source_segment_index,
        source_timestamp_seconds=task.source_timestamp_seconds,
        due_at=task.due_at,
        priority=task.priority,
        status=task.status,
        assignment_email_status=task.assignment_email_status,
        assignment_email_error=task.assignment_email_error,
        created_at=task.created_at,
    )


def _task_row(db: Session, task_id: str) -> tuple[Task, Employee, Meeting] | None:
    return _task_query(db).filter(Task.id == task_id).one_or_none()


@router.get("/", response_model=list[TaskResponse])
def get_assigned_tasks(
    assignee_email: str = Query(min_length=1),
    db: Session = Depends(get_db),
):
    rows = _task_query(db).filter(Employee.email == assignee_email).all()
    return [_task_response(*row) for row in rows]


@router.get("/dashboard", response_model=list[TaskResponse])
def get_task_dashboard(db: Session = Depends(get_db)):
    rows = _task_query(db).all()
    return [_task_response(*row) for row in rows]


@router.get("/calendar.ics")
def get_task_calendar(
    assignee_email: str = Query(min_length=1),
    db: Session = Depends(get_db),
):
    rows = _task_query(db).filter(Employee.email == assignee_email).all()
    events = []
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for task, _, meeting in rows:
        due_at = task.due_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        due_stamp = due_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        description = _escape_ical_text(
            f"Priority: {task.priority}; Source meeting: {meeting.title}; "
            f"Transcript timestamp: {task.source_timestamp_seconds:.2f}s"
        )
        events.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{task.id}@ai-office-operations",
                f"DTSTAMP:{now}",
                f"DTSTART:{due_stamp}",
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
        attempt_assignment_email(db, task_id)
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
    return _task_response(*row)
