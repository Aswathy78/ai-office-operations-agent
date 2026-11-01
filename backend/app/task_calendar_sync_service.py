"""Keep task deadlines in the assignee's calendar (Story 5.5 / FR-28).

Every open task gets one deadline entry (with a reminder) in the assignee's
calendar through the configured ``CalendarProvider``. Each task has exactly one
calendar sync status::

    pending ──claimed──> processing ──read-back matches──> completed
                         processing ──error, attempts left──> retrying ──backoff──> processing
                         processing ──error, attempts used up──> failed ──user retries──> pending
    completed / failed ──task or calendar changed──> pending

* A task without a sync record, or whose due date / title / assignee differs from
  what was last synced, is (re)queued as ``pending`` by :func:`reconcile`.
* ``completed`` is only set after the provider's copy was read back and matched the
  task; any error or mismatch is ``retrying`` or ``failed`` — never ``completed``.
* :func:`poll_external_changes` (every 5 minutes) reads each synced entry from the
  provider. If someone moved, renamed or deleted it in the calendar, the change is
  recorded and the task's deadline is written back: the task stays the source of
  truth, because only managers may change due dates (Story 3.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import os
import uuid

from sqlalchemy.orm import Session

from app.meeting.calendar_provider import CalendarProviderError, get_calendar_provider
from app.meeting.models import (
    CALENDAR_SYNC_STATUS_COMPLETED,
    CALENDAR_SYNC_STATUS_FAILED,
    CALENDAR_SYNC_STATUS_PENDING,
    CALENDAR_SYNC_STATUS_PROCESSING,
    CALENDAR_SYNC_STATUS_RETRYING,
    Employee,
    Task,
    TaskCalendarSync,
    TASK_STATUS_PENDING,
)

logger = logging.getLogger(__name__)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    CALENDAR_SYNC_STATUS_PENDING: frozenset({CALENDAR_SYNC_STATUS_PROCESSING}),
    CALENDAR_SYNC_STATUS_PROCESSING: frozenset({
        CALENDAR_SYNC_STATUS_COMPLETED,
        CALENDAR_SYNC_STATUS_RETRYING,
        CALENDAR_SYNC_STATUS_FAILED,
    }),
    CALENDAR_SYNC_STATUS_RETRYING: frozenset({CALENDAR_SYNC_STATUS_PROCESSING, CALENDAR_SYNC_STATUS_PENDING}),
    CALENDAR_SYNC_STATUS_COMPLETED: frozenset({CALENDAR_SYNC_STATUS_PENDING}),
    CALENDAR_SYNC_STATUS_FAILED: frozenset({CALENDAR_SYNC_STATUS_PENDING}),
}

# Errors from the provider (or its network) that are retried with backoff.
SYNC_ERRORS = (CalendarProviderError, OSError, TimeoutError, ValueError)


class InvalidCalendarSyncTransition(Exception):
    """The status change is not part of the calendar sync lifecycle."""


class CalendarSyncMismatch(CalendarProviderError):
    """The provider's copy does not match what was written (never reported as completed)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    left, right = _as_utc(left), _as_utc(right)
    if left is None or right is None:
        return left is right
    return abs((left - right).total_seconds()) < 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def max_attempts() -> int:
    try:
        return max(1, int(os.getenv("CALENDAR_SYNC_MAX_ATTEMPTS", "5")))
    except ValueError:
        return 5


def backoff_seconds(attempt: int) -> int:
    """Wait after failed attempt ``attempt``: 1, 5, 15 minutes, then 15."""
    raw = os.getenv("CALENDAR_SYNC_BACKOFF_MINUTES", "")
    try:
        schedule = tuple(float(p) for p in raw.split(",") if p.strip())
    except ValueError:
        schedule = ()
    schedule = tuple(m for m in schedule if m > 0) or (1, 5, 15)
    return int(schedule[min(max(attempt, 1), len(schedule)) - 1] * 60)


def reminder_minutes_before() -> int:
    try:
        return max(0, int(os.getenv("CALENDAR_DEADLINE_REMINDER_MINUTES", "60")))
    except ValueError:
        return 60


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------


def transition(sync: TaskCalendarSync, to_status: str, *, now: datetime | None = None) -> None:
    if to_status not in ALLOWED_TRANSITIONS.get(sync.status, frozenset()):
        raise InvalidCalendarSyncTransition(
            f"Calendar sync cannot move from {sync.status} to {to_status}."
        )
    at = now or _now()
    sync.status = to_status
    sync.status_changed_at = at
    sync.updated_at = at


def _queue(sync: TaskCalendarSync, reason: str) -> None:
    """(Re)queue a sync for the next pass, starting a fresh set of attempts."""
    if sync.status in (CALENDAR_SYNC_STATUS_PENDING, CALENDAR_SYNC_STATUS_PROCESSING):
        sync.pending_reason = reason
        return
    transition(sync, CALENDAR_SYNC_STATUS_PENDING)
    sync.pending_reason = reason
    sync.attempt_count = 0
    sync.next_attempt_at = None
    sync.last_error = None


@dataclass
class SyncSummary:
    queued: int = 0
    completed: int = 0
    retrying: int = 0
    failed: int = 0
    external_changes: int = 0
    polled: int = 0
    task_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "queued": self.queued,
            "completed": self.completed,
            "retrying": self.retrying,
            "failed": self.failed,
            "external_changes": self.external_changes,
            "polled": self.polled,
            "task_ids": list(self.task_ids),
        }


# ---------------------------------------------------------------------------
# Reconcile tasks -> sync records
# ---------------------------------------------------------------------------


def _change_reason(task: Task, sync: TaskCalendarSync) -> str | None:
    if sync.synced_assignee_id and sync.synced_assignee_id != task.assignee_id:
        return "Task reassigned"
    if sync.synced_due_at is not None and not _same_instant(sync.synced_due_at, task.due_at):
        return "Due date changed"
    if sync.synced_title is not None and sync.synced_title != task.title:
        return "Task title changed"
    return None


def reconcile(db: Session, summary: SyncSummary | None = None) -> SyncSummary:
    """Make sure every open task has a sync record, and queue the ones that changed."""
    summary = summary or SyncSummary()
    syncs = {s.task_id: s for s in db.query(TaskCalendarSync).all()}
    now = _now()
    for task in db.query(Task).filter(Task.status == TASK_STATUS_PENDING).all():
        sync = syncs.get(task.id)
        if sync is None:
            db.add(TaskCalendarSync(
                id=str(uuid.uuid4()),
                task_id=task.id,
                provider=get_calendar_provider().provider_name,
                status=CALENDAR_SYNC_STATUS_PENDING,
                status_changed_at=now,
                pending_reason="New task deadline",
                attempt_count=0,
                created_at=now,
                updated_at=now,
            ))
            summary.queued += 1
            continue
        if sync.status in (CALENDAR_SYNC_STATUS_COMPLETED, CALENDAR_SYNC_STATUS_FAILED,
                           CALENDAR_SYNC_STATUS_RETRYING):
            reason = _change_reason(task, sync)
            if reason and sync.status == CALENDAR_SYNC_STATUS_COMPLETED:
                _queue(sync, reason)
                summary.queued += 1
            elif reason and sync.status in (CALENDAR_SYNC_STATUS_FAILED, CALENDAR_SYNC_STATUS_RETRYING) \
                    and task.updated_at is not None and sync.status_changed_at is not None \
                    and _as_utc(task.updated_at) > _as_utc(sync.status_changed_at):
                # The task changed again after the sync went wrong: start fresh attempts.
                _queue(sync, reason)
                summary.queued += 1
    db.commit()
    return summary


def request_sync(db: Session, task_id: str, reason: str) -> TaskCalendarSync | None:
    """Queue one task now (e.g. right after its due date changed)."""
    task = db.get(Task, task_id)
    if task is None:
        return None
    sync = db.query(TaskCalendarSync).filter(TaskCalendarSync.task_id == task_id).one_or_none()
    if sync is None:
        reconcile(db)
        return db.query(TaskCalendarSync).filter(TaskCalendarSync.task_id == task_id).one_or_none()
    _queue(sync, reason)
    db.commit()
    return sync


def retry_failed(db: Session, task_id: str) -> TaskCalendarSync:
    """Failed -> Pending on request (a fresh set of attempts)."""
    sync = db.query(TaskCalendarSync).filter(TaskCalendarSync.task_id == task_id).one_or_none()
    if sync is None:
        raise LookupError("This task has no calendar sync yet.")
    if sync.status != CALENDAR_SYNC_STATUS_FAILED:
        raise InvalidCalendarSyncTransition(f"Only a failed calendar sync can be retried (it is {sync.status}).")
    _queue(sync, "Retry requested")
    db.commit()
    db.refresh(sync)
    return sync


# ---------------------------------------------------------------------------
# Push to the provider
# ---------------------------------------------------------------------------


def _claim(db: Session, sync_id: str, from_status: str) -> bool:
    now = _now()
    claimed = (
        db.query(TaskCalendarSync)
        .filter(TaskCalendarSync.id == sync_id, TaskCalendarSync.status == from_status)
        .update(
            {
                TaskCalendarSync.status: CALENDAR_SYNC_STATUS_PROCESSING,
                TaskCalendarSync.status_changed_at: now,
                TaskCalendarSync.updated_at: now,
                TaskCalendarSync.attempt_count: TaskCalendarSync.attempt_count + 1,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return claimed == 1


def _push(db: Session, sync: TaskCalendarSync, task: Task, assignee: Employee) -> None:
    """Write the task's deadline to the provider and verify it by reading it back."""
    provider = get_calendar_provider()
    title = f"Task due: {task.title}"
    description = f"Deadline for a task assigned to {assignee.name}. Priority: {task.priority}."
    previous_owner = sync.synced_owner_email
    entry_id = sync.external_entry_id
    if entry_id and previous_owner and previous_owner.casefold() != assignee.email.casefold():
        # Reassigned: remove it from the old assignee's calendar, create it in the new one.
        provider.delete_deadline_entry(db, entry_id)
        entry_id = None

    written = provider.upsert_deadline_entry(
        db,
        entry_id=entry_id,
        owner_email=assignee.email,
        title=title,
        due_at=task.due_at,
        description=description,
        reminder_minutes_before=reminder_minutes_before(),
        external_ref=f"task:{task.id}",
    )
    read_back = provider.get_deadline_entry(db, written.id)
    if (
        read_back is None
        or read_back.deleted
        or not _same_instant(read_back.due_at, task.due_at)
        or read_back.title != title
        or read_back.owner_email.casefold() != assignee.email.casefold()
    ):
        raise CalendarSyncMismatch("The calendar entry could not be verified after writing it.")

    sync.provider = provider.provider_name
    sync.external_entry_id = read_back.id
    sync.synced_due_at = _as_utc(task.due_at)
    sync.synced_title = task.title
    sync.synced_assignee_id = task.assignee_id
    sync.synced_owner_email = assignee.email
    sync.synced_sequence = task.calendar_sequence or 0
    sync.synced_entry_version = read_back.version


def _sync_one(db: Session, sync_id: str, summary: SyncSummary) -> None:
    sync = db.get(TaskCalendarSync, sync_id)
    db.refresh(sync)
    task = db.get(Task, sync.task_id)
    assignee = db.get(Employee, task.assignee_id) if task else None
    try:
        if task is None or assignee is None or not assignee.email:
            raise CalendarProviderError("The task or its assignee no longer exists.")
        _push(db, sync, task, assignee)
    except SYNC_ERRORS as exc:
        db.rollback()
        sync = db.get(TaskCalendarSync, sync_id)
        db.refresh(sync)
        sync.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        if sync.attempt_count < max_attempts():
            transition(sync, CALENDAR_SYNC_STATUS_RETRYING)
            sync.next_attempt_at = _now() + timedelta(seconds=backoff_seconds(sync.attempt_count))
            summary.retrying += 1
        else:
            transition(sync, CALENDAR_SYNC_STATUS_FAILED)
            sync.next_attempt_at = None
            summary.failed += 1
        logger.warning("Calendar sync failed task_id=%s attempt=%s: %s", sync.task_id, sync.attempt_count, exc)
    except Exception as exc:  # noqa: BLE001 - never leave a sync stuck in processing
        db.rollback()
        sync = db.get(TaskCalendarSync, sync_id)
        db.refresh(sync)
        sync.last_error = f"Unexpected error: {exc}"[:1000]
        sync.next_attempt_at = None
        transition(sync, CALENDAR_SYNC_STATUS_FAILED)
        summary.failed += 1
        logger.exception("Calendar sync crashed task_id=%s", sync.task_id)
    else:
        transition(sync, CALENDAR_SYNC_STATUS_COMPLETED)
        sync.last_error = None
        sync.next_attempt_at = None
        sync.pending_reason = None
        sync.last_synced_at = _now()
        summary.completed += 1
    summary.task_ids.append(sync.task_id)
    db.commit()
    if sync.status == CALENDAR_SYNC_STATUS_FAILED:
        _notify_calendar_failure(db, sync)


def _notify_calendar_failure(db: Session, sync: TaskCalendarSync) -> None:
    task = db.get(Task, sync.task_id)
    employee = db.get(Employee, task.assignee_id) if task else None
    if task is None or employee is None or not employee.is_active:
        return
    from app.notification_service import notify_workflow_failure

    notify_workflow_failure(
        db,
        recipient=employee,
        summary=f"Calendar sync failed for task: {task.title}. {sync.last_error or ''}".strip(),
        idempotency_key=f"calendar-sync-failure:{sync.id}:a{sync.attempt_count}:{employee.id}",
        entity_type="task",
        entity_id=task.id,
        entity_url=f"/tasks?task_id={task.id}",
    )


def process_due(db: Session, now: datetime | None = None, limit: int = 50) -> SyncSummary:
    """Push every pending sync and every retry whose backoff has elapsed."""
    now = _as_utc(now or _now())
    summary = SyncSummary()
    due = (
        db.query(TaskCalendarSync.id, TaskCalendarSync.status)
        .filter(
            (TaskCalendarSync.status == CALENDAR_SYNC_STATUS_PENDING)
            | ((TaskCalendarSync.status == CALENDAR_SYNC_STATUS_RETRYING)
               & (TaskCalendarSync.next_attempt_at <= now))
        )
        .limit(limit)
        .all()
    )
    for sync_id, status in due:
        if _claim(db, sync_id, status):
            _sync_one(db, sync_id, summary)
    return summary


def recover_stuck_processing(db: Session, older_than_seconds: int = 300) -> int:
    """A sync left in processing by a crashed process is retried (never assumed completed)."""
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    stuck = (
        db.query(TaskCalendarSync)
        .filter(TaskCalendarSync.status == CALENDAR_SYNC_STATUS_PROCESSING,
                TaskCalendarSync.status_changed_at < cutoff)
        .all()
    )
    terminal_failures: list[TaskCalendarSync] = []
    for sync in stuck:
        sync.last_error = "Interrupted while syncing; will retry."
        if sync.attempt_count < max_attempts():
            transition(sync, CALENDAR_SYNC_STATUS_RETRYING)
            sync.next_attempt_at = _now()
        else:
            transition(sync, CALENDAR_SYNC_STATUS_FAILED)
            terminal_failures.append(sync)
    if stuck:
        db.commit()
    for sync in terminal_failures:
        _notify_calendar_failure(db, sync)
    return len(stuck)


# ---------------------------------------------------------------------------
# Poll the provider for external changes
# ---------------------------------------------------------------------------


def poll_external_changes(db: Session, summary: SyncSummary | None = None) -> SyncSummary:
    """Read every synced entry back; queue a re-sync when it was changed in the calendar."""
    summary = summary or SyncSummary()
    provider = get_calendar_provider()
    now = _now()
    synced = (
        db.query(TaskCalendarSync)
        .filter(TaskCalendarSync.status == CALENDAR_SYNC_STATUS_COMPLETED,
                TaskCalendarSync.external_entry_id.isnot(None))
        .all()
    )
    for sync in synced:
        summary.polled += 1
        sync.last_polled_at = now
        try:
            entry = provider.get_deadline_entry(db, sync.external_entry_id)
        except SYNC_ERRORS as exc:
            # Could not look; the last confirmed sync still stands, but say so.
            sync.last_poll_error = f"{type(exc).__name__}: {exc}"[:500]
            continue
        sync.last_poll_error = None
        detail = None
        if entry is None or entry.deleted:
            detail = "The deadline was deleted from the calendar; it has been restored."
        elif not _same_instant(entry.due_at, sync.synced_due_at):
            moved_to = _as_utc(entry.due_at).strftime("%d %b %Y %H:%M UTC")
            detail = (f"The deadline was moved to {moved_to} in the calendar; "
                      "it has been set back to the task's due date.")
        elif entry.version != sync.synced_entry_version:
            detail = "The calendar entry was edited outside the app; it has been refreshed."
        if detail:
            sync.external_change_detected_at = now
            sync.external_change_detail = detail
            _queue(sync, "Changed in the calendar")
            summary.external_changes += 1
    db.commit()
    return summary


def run_sync_pass(db: Session, *, poll: bool = False) -> SyncSummary:
    """One scheduler pass: reconcile, (optionally) poll the provider, then push due syncs."""
    summary = reconcile(db)
    recover_stuck_processing(db)
    if poll:
        poll_external_changes(db, summary)
    pushed = process_due(db)
    summary.completed += pushed.completed
    summary.retrying += pushed.retrying
    summary.failed += pushed.failed
    summary.task_ids.extend(pushed.task_ids)
    return summary
