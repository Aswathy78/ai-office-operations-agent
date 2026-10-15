"""Background scheduler that runs task reminders / escalations periodically (Story 4.4).

Configuration (backend/.env):
- ``TASK_REMINDER_SCHEDULER_ENABLED`` (default ``true``)
- ``TASK_REMINDER_INTERVAL_SECONDS`` (default ``300``; minimum 30)

Duplicate e-mails are prevented by idempotency keys in ``task_notifications``,
so overlapping runs (e.g. two server processes) are safe.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
_run_lock = threading.Lock()


def scheduler_enabled() -> bool:
    value = os.getenv("TASK_REMINDER_SCHEDULER_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def scheduler_interval_seconds() -> int:
    try:
        return max(30, int(os.getenv("TASK_REMINDER_INTERVAL_SECONDS", "300")))
    except ValueError:
        return 300


def run_once() -> dict:
    """Run a single reminder pass with its own database session."""
    from app.db.database import SessionLocal
    from app.task_reminder_service import run_task_reminders

    # Serialise passes inside this process (scheduler tick vs. manual trigger).
    with _run_lock, SessionLocal() as db:
        summary = run_task_reminders(db).as_dict()
    if summary["reminders_sent"] or summary["escalations_sent"] or summary["failed"]:
        logger.info("Task reminder pass: %s", summary)
    return summary


def _loop(stop: threading.Event, interval: int) -> None:
    # Short delay so startup (migrations, seeding) finishes first.
    if stop.wait(5):
        return
    while not stop.is_set():
        try:
            run_once()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            logger.exception("Task reminder pass failed")
        stop.wait(interval)


def start_task_reminder_scheduler() -> bool:
    """Start the background thread once. Returns whether a scheduler is running."""
    global _stop_event, _thread
    if not scheduler_enabled():
        logger.info("Task reminder scheduler disabled (TASK_REMINDER_SCHEDULER_ENABLED).")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_loop,
        args=(_stop_event, scheduler_interval_seconds()),
        name="task-reminder-scheduler",
        daemon=True,
    )
    _thread.start()
    logger.info("Task reminder scheduler started (every %ss).", scheduler_interval_seconds())
    return True


def stop_task_reminder_scheduler() -> None:
    global _thread
    if _stop_event is not None:
        _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=10)
    _thread = None
