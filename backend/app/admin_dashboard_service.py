"""Administrator dashboard data: users, departments, integrations and system health (Story 9.4).

Everything here is read from the live system: the database, the background threads and the
request metrics the API records about itself. Nothing is configured or hard-coded.

Two rules shape the module:

* **Each section stands alone.** If one cannot be read, that section reports
  ``available: false`` with a plain-language reason and the rest of the dashboard still
  loads. A dashboard that goes blank because one query failed would hide the very
  problem it exists to show.
* **No secrets.** Passwords, hashes, tokens, SMTP user names and raw provider errors never
  leave this module. Integrations report counts and states, not credentials or messages.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import time
from typing import Callable

from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from app.meeting.models import (
    AuthAccount,
    AuthSession,
    CALENDAR_SYNC_STATUS_COMPLETED,
    CALENDAR_SYNC_STATUS_FAILED,
    CALENDAR_SYNC_STATUS_RETRYING,
    EMAIL_DRAFT_STATUS_FAILED,
    EMAIL_DRAFT_STATUS_RETRY,
    EMAIL_DRAFT_STATUS_SENT,
    Employee,
    EmailDraft,
    MeetingSummary,
    MeetingTranscript,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
    TaskCalendarSync,
    WorkflowDeadLetter,
    WorkflowRun,
)
from app.system_metrics import request_metrics

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30
USER_LIST_LIMIT = 200
ACTIVITY_WINDOW = timedelta(hours=24)
ERROR_RATE_ALERT_PERCENT = 5.0
UNASSIGNED_DEPARTMENT = "Unassigned"

# Integration / overall states, worst last. The front end maps these to colours and text.
STATUS_OPERATIONAL = "operational"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_UNKNOWN = "unknown"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _section(name: str, build: Callable[[], dict]) -> dict:
    """Run one collector; a failure becomes an 'unavailable' section, never an exception."""
    try:
        return {"available": True, "error": None, **build()}
    except Exception as error:  # noqa: BLE001 - one broken collector must not blank the page
        logger.warning("Admin dashboard section %s failed: %s", name, error)
        return {
            "available": False,
            "error": f"{name.replace('_', ' ').capitalize()} could not be read right now.",
        }


# ---------------------------------------------------------------------------
# Users and departments
# ---------------------------------------------------------------------------


def collect_users(db: Session) -> dict:
    last_sign_in = (
        db.query(AuthSession.account_id, func.max(AuthSession.created_at).label("last_at"))
        .group_by(AuthSession.account_id)
        .subquery()
    )
    rows = (
        db.query(Employee, AuthAccount, last_sign_in.c.last_at)
        .outerjoin(AuthAccount, AuthAccount.employee_id == Employee.id)
        .outerjoin(last_sign_in, last_sign_in.c.account_id == AuthAccount.id)
        .order_by(Employee.name.asc(), Employee.id.asc())
        .all()
    )
    by_role: dict[str, int] = {}
    active = 0
    items = []
    for employee, account, last_at in rows:
        is_active = bool(account and account.is_enabled and employee.is_active)
        active += int(is_active)
        role = account.role if account else None
        if role:
            by_role[role] = by_role.get(role, 0) + 1
        if len(items) < USER_LIST_LIMIT:
            items.append(
                {
                    "id": employee.id,
                    "name": employee.name,
                    "email": employee.email,
                    "role": role,
                    "department": employee.department or UNASSIGNED_DEPARTMENT,
                    "is_active": is_active,
                    "login_status": (
                        "No account"
                        if account is None
                        else "Enabled" if is_active else "Disabled"
                    ),
                    "last_sign_in_at": _iso(last_at),
                }
            )
    return {
        "total": len(rows),
        "active": active,
        "disabled": len(rows) - active,
        "by_role": by_role,
        "items": items,
        "truncated": len(rows) > USER_LIST_LIMIT,
    }


def collect_departments(db: Session) -> dict:
    department = func.coalesce(
        func.nullif(func.trim(Employee.department), ""),
        UNASSIGNED_DEPARTMENT,
    )
    with_login = (
        db.query(AuthAccount.employee_id)
        .filter(AuthAccount.is_enabled.is_(True))
        .subquery()
    )
    rows = (
        db.query(
            department.label("name"),
            func.count(Employee.id),
            func.sum(case((Employee.is_active.is_(True), 1), else_=0)),
            func.count(with_login.c.employee_id),
        )
        .outerjoin(with_login, with_login.c.employee_id == Employee.id)
        .group_by(department)
        .order_by(department.asc())
        .all()
    )
    items = [
        {
            "name": name,
            "employees": int(total),
            "active_employees": int(active or 0),
            "with_login": int(logins),
        }
        for name, total, active, logins in rows
    ]
    return {"total": len(items), "items": items}


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


def _outcome_status(succeeded: int, failed: int, *, idle: str = STATUS_UNKNOWN) -> str:
    """State of an integration from what really happened to its work in the last day."""
    if failed == 0:
        return STATUS_OPERATIONAL if succeeded else idle
    if succeeded == 0 or failed >= succeeded:
        return STATUS_DOWN
    return STATUS_DEGRADED


def _integration(key: str, name: str, status: str, detail: str, **metrics) -> dict:
    return {"key": key, "name": name, "status": status, "detail": detail, "metrics": metrics}


def _email_integration(db: Session, since: datetime) -> dict:
    from app.email_providers import fallback_settings, primary_settings

    primary = primary_settings()
    if not (primary.host and primary.username and primary.password):
        return _integration(
            "email", "E-mail (SMTP)", STATUS_NOT_CONFIGURED,
            "No mail account is configured, so e-mail cannot be sent.",
        )
    counts = dict(
        db.query(EmailDraft.status, func.count(EmailDraft.id))
        .filter(
            EmailDraft.status.in_(
                (EMAIL_DRAFT_STATUS_SENT, EMAIL_DRAFT_STATUS_FAILED, EMAIL_DRAFT_STATUS_RETRY)
            ),
            EmailDraft.updated_at >= since,
        )
        .group_by(EmailDraft.status)
        .all()
    )
    sent = counts.get(EMAIL_DRAFT_STATUS_SENT, 0)
    failed = counts.get(EMAIL_DRAFT_STATUS_FAILED, 0)
    waiting = counts.get(EMAIL_DRAFT_STATUS_RETRY, 0)
    status = _outcome_status(sent, failed)
    detail = {
        STATUS_OPERATIONAL: "E-mail sent normally in the last 24 hours.",
        STATUS_DEGRADED: "Some e-mail failed in the last 24 hours.",
        STATUS_DOWN: "Most e-mail failed in the last 24 hours. Check the mail account settings.",
        STATUS_UNKNOWN: "Configured. No e-mail has been sent in the last 24 hours, so health is not known.",
    }[status]
    return _integration(
        "email", "E-mail (SMTP)", status, detail,
        provider=primary.name, fallback_configured=fallback_settings() is not None,
        sent_24h=sent, failed_24h=failed, waiting_to_retry=waiting,
    )


def _ai_integration(db: Session, since: datetime) -> dict:
    if not (os.getenv("GROQ_API_KEY") or "").strip():
        return _integration(
            "ai_model", "AI model (Groq)", STATUS_NOT_CONFIGURED,
            "No AI key is configured. The assistant runs in local mode.",
        )
    counts = dict(
        db.query(MeetingSummary.status, func.count(MeetingSummary.id))
        .filter(MeetingSummary.updated_at >= since, MeetingSummary.status.in_((SUMMARY_STATUS_COMPLETED, "failed")))
        .group_by(MeetingSummary.status)
        .all()
    )
    done = counts.get(SUMMARY_STATUS_COMPLETED, 0)
    failed = counts.get("failed", 0)
    status = _outcome_status(done, failed)
    detail = {
        STATUS_OPERATIONAL: "Meeting summaries completed in the last 24 hours.",
        STATUS_DEGRADED: "Some meeting summaries failed in the last 24 hours.",
        STATUS_DOWN: "Meeting summaries are failing. Check the AI key and the provider's status.",
        STATUS_UNKNOWN: "Key configured. No AI work ran in the last 24 hours, so health is not known.",
    }[status]
    return _integration(
        "ai_model", "AI model (Groq)", status, detail, summaries_24h=done, failed_24h=failed
    )


def _calendar_integration(db: Session, since: datetime) -> dict:
    counts = dict(
        db.query(TaskCalendarSync.status, func.count(TaskCalendarSync.id))
        .filter(TaskCalendarSync.status_changed_at >= since)
        .group_by(TaskCalendarSync.status)
        .all()
    )
    done = counts.get(CALENDAR_SYNC_STATUS_COMPLETED, 0)
    failed = counts.get(CALENDAR_SYNC_STATUS_FAILED, 0)
    retrying = db.query(func.count(TaskCalendarSync.id)).filter(
        TaskCalendarSync.status == CALENDAR_SYNC_STATUS_RETRYING
    ).scalar() or 0
    # The local calendar lives in this database, so "nothing to sync" is healthy.
    status = _outcome_status(done, failed, idle=STATUS_OPERATIONAL)
    detail = {
        STATUS_OPERATIONAL: "Calendar entries are in sync.",
        STATUS_DEGRADED: "Some calendar syncs failed in the last 24 hours.",
        STATUS_DOWN: "Calendar syncs are failing.",
    }[status]
    return _integration(
        "calendar", "Calendar", status, detail,
        synced_24h=done, failed_24h=failed, retrying=int(retrying),
    )


def _transcription_integration(db: Session, since: datetime) -> dict:
    from app.meeting import transcription_worker

    enabled = transcription_worker.background_worker_enabled()
    thread = getattr(transcription_worker, "_background_thread", None)
    running = bool(thread is not None and thread.is_alive())
    counts = dict(
        db.query(MeetingTranscript.status, func.count(MeetingTranscript.id))
        .filter(
            func.coalesce(MeetingTranscript.completed_at, MeetingTranscript.created_at) >= since,
            MeetingTranscript.status.in_((TRANSCRIPT_STATUS_COMPLETED, TRANSCRIPT_STATUS_FAILED)),
        )
        .group_by(MeetingTranscript.status)
        .all()
    )
    done = counts.get(TRANSCRIPT_STATUS_COMPLETED, 0)
    failed = counts.get(TRANSCRIPT_STATUS_FAILED, 0)
    if not enabled:
        status, detail = STATUS_NOT_CONFIGURED, "Background transcription is turned off."
    elif not running:
        status, detail = STATUS_DOWN, "The transcription worker is not running."
    else:
        status = _outcome_status(done, failed, idle=STATUS_OPERATIONAL)
        detail = {
            STATUS_OPERATIONAL: "Transcription is running.",
            STATUS_DEGRADED: "Some transcripts failed in the last 24 hours.",
            STATUS_DOWN: "Transcripts are failing.",
        }[status]
    return _integration(
        "transcription", "Transcription (Whisper)", status, detail,
        worker_running=running, transcribed_24h=done, failed_24h=failed,
    )


def _ocr_integration() -> dict:
    from app.documents import processor
    from app.documents.extraction import tesseract_path

    thread = getattr(processor, "_worker_thread", None)
    running = bool(thread is not None and thread.is_alive())
    ocr = tesseract_path() is not None
    if not running:
        status, detail = STATUS_DOWN, "The document processor is not running."
    elif not ocr:
        status, detail = STATUS_DEGRADED, "Documents are processed, but images and scanned PDFs cannot be read (OCR engine missing)."
    else:
        status, detail = STATUS_OPERATIONAL, "Document processing and OCR are available."
    return _integration(
        "documents", "Document processing", status, detail,
        processor_running=running, ocr_available=ocr,
    )


def collect_integrations(db: Session) -> dict:
    since = datetime.now(timezone.utc) - ACTIVITY_WINDOW
    collectors = (
        ("email", lambda: _email_integration(db, since)),
        ("ai_model", lambda: _ai_integration(db, since)),
        ("calendar", lambda: _calendar_integration(db, since)),
        ("transcription", lambda: _transcription_integration(db, since)),
        ("documents", _ocr_integration),
    )
    items = []
    for key, collect in collectors:
        try:
            items.append(collect())
        except Exception as error:  # noqa: BLE001 - one integration must not hide the others
            logger.warning("Integration check %s failed: %s", key, error)
            items.append(
                _integration(key, key.replace("_", " ").capitalize(), STATUS_UNKNOWN,
                             "This integration could not be checked right now.")
            )
    return {"items": items, "window_hours": int(ACTIVITY_WINDOW.total_seconds() // 3600)}


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------


def _database_health(db: Session) -> dict:
    started = time.perf_counter()
    db.execute(text("SELECT 1"))
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    schema_current: bool | None
    try:
        from app.db.database import engine
        from app.db.migrate import current_revision, head_revision

        schema_current = current_revision(engine) == head_revision()
    except Exception:  # noqa: BLE001 - schema state is informational
        schema_current = None
    return {"latency_ms": latency_ms, "schema_current": schema_current}


def _queue_health(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    waiting = db.query(WorkflowRun).filter(WorkflowRun.status.in_(("pending", "retrying")))
    due = waiting.filter(
        (WorkflowRun.next_attempt_at.is_(None)) | (WorkflowRun.next_attempt_at <= now)
    )
    oldest = db.query(func.min(WorkflowRun.created_at)).filter(
        WorkflowRun.status.in_(("pending", "retrying"))
    ).scalar()
    oldest = _aware(oldest)
    return {
        "depth": waiting.count(),
        "due_now": due.count(),
        "processing": db.query(WorkflowRun).filter(WorkflowRun.status == "processing").count(),
        "oldest_waiting_seconds": max(0, round((now - oldest).total_seconds())) if oldest else None,
        "failed_24h": db.query(WorkflowRun)
        .filter(WorkflowRun.status == "failed", WorkflowRun.updated_at >= now - ACTIVITY_WINDOW)
        .count(),
        "dead_letter_open": db.query(func.count(WorkflowDeadLetter.id))
        .filter(WorkflowDeadLetter.status == "open")
        .scalar()
        or 0,
    }


def _background_services() -> list[dict]:
    """Whether each long-running background thread of the API is alive."""
    from app import email_dispatcher, task_calendar_scheduler, task_reminder_scheduler
    from app.documents import processor
    from app.meeting import transcription_worker

    def alive(module, attribute: str) -> bool:
        thread = getattr(module, attribute, None)
        return bool(thread is not None and thread.is_alive())

    services = [
        ("Task reminders", alive(task_reminder_scheduler, "_thread")),
        ("E-mail dispatcher", alive(email_dispatcher, "_thread")),
        ("Calendar sync", alive(task_calendar_scheduler, "_thread")),
        ("Document processor", alive(processor, "_worker_thread")),
    ]
    if transcription_worker.background_worker_enabled():
        services.append(("Transcription", alive(transcription_worker, "_background_thread")))
    return [{"name": name, "running": running} for name, running in services]


def collect_system(db: Session) -> dict:
    from app.workflow_worker import worker_status

    return {
        "database": _database_health(db),
        "api": request_metrics.snapshot(),
        "queue": _queue_health(db),
        "workers": worker_status(),
        "background_services": _background_services(),
    }


# ---------------------------------------------------------------------------
# The whole dashboard
# ---------------------------------------------------------------------------


def _alerts(sections: dict) -> list[str]:
    alerts: list[str] = []
    for key, label in (("users", "Users"), ("departments", "Departments"),
                       ("integrations", "Integrations"), ("system", "System metrics")):
        if not sections[key]["available"]:
            alerts.append(f"{label} could not be read.")
    system = sections["system"]
    if system["available"]:
        api = system["api"]
        rate = api["error_rate_percent"]
        if rate is not None and rate >= ERROR_RATE_ALERT_PERCENT:
            alerts.append(f"{rate:g}% of recent API requests failed with a server error.")
        queue, workers = system["queue"], system["workers"]
        if workers["alive"] < workers["configured"]:
            alerts.append(f"Only {workers['alive']} of {workers['configured']} workflow workers are running.")
        if queue["depth"] > 0 and workers["alive"] == 0:
            alerts.append("Workflows are waiting but no worker is running.")
        if queue["dead_letter_open"]:
            alerts.append(f"{queue['dead_letter_open']} workflow run(s) are in the dead-letter queue.")
        for service in system["background_services"]:
            if not service["running"]:
                alerts.append(f"{service['name']} is not running.")
        if system["database"]["schema_current"] is False:
            alerts.append("The database is behind the code. Restart the API to upgrade it.")
    if sections["integrations"]["available"]:
        for item in sections["integrations"]["items"]:
            if item["status"] in (STATUS_DEGRADED, STATUS_DOWN):
                alerts.append(f"{item['name']}: {item['detail']}")
    return alerts


def overall_status(sections: dict, alerts: list[str]) -> str:
    # Without the system section nothing about health is known; say so rather than guess.
    if not sections["system"]["available"]:
        return STATUS_UNKNOWN
    return STATUS_DEGRADED if alerts else STATUS_OPERATIONAL


def build_dashboard(db: Session) -> dict:
    sections = {
        "users": _section("users", lambda: collect_users(db)),
        "departments": _section("departments", lambda: collect_departments(db)),
        "integrations": _section("integrations", lambda: collect_integrations(db)),
        "system": _section("system", lambda: collect_system(db)),
    }
    alerts = _alerts(sections)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "overall_status": overall_status(sections, alerts),
        "alerts": alerts,
        **sections,
    }
