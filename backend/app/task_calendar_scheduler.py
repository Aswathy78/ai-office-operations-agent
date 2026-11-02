"""Background scheduler for task deadline calendar sync (Story 5.5 / FR-28).

Configuration (backend/.env):
- ``TASK_CALENDAR_SYNC_ENABLED`` (default ``true``)
- ``TASK_CALENDAR_SYNC_INTERVAL_SECONDS`` (default ``15``): push new / changed deadlines
- ``TASK_CALENDAR_POLL_INTERVAL_SECONDS`` (default ``300``): poll the provider for
  external changes (FR-28: every 5 minutes)
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_stop_event: threading.Event | None = None
_wake_event = threading.Event()
_thread: threading.Thread | None = None


def scheduler_enabled() -> bool:
    return os.getenv("TASK_CALENDAR_SYNC_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _seconds(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def sync_interval_seconds() -> float:
    return _seconds("TASK_CALENDAR_SYNC_INTERVAL_SECONDS", 15, 1)


def poll_interval_seconds() -> float:
    return _seconds("TASK_CALENDAR_POLL_INTERVAL_SECONDS", 300, 5)


def wake() -> None:
    """Sync now (e.g. right after a due-date change) instead of waiting for the interval."""
    _wake_event.set()


def run_once(*, poll: bool = False) -> dict:
    from app.db.database import SessionLocal
    from app.task_calendar_sync_service import run_sync_pass

    with SessionLocal() as db:
        summary = run_sync_pass(db, poll=poll).as_dict()
    if summary["completed"] or summary["failed"] or summary["external_changes"]:
        logger.info("Task calendar sync pass: %s", summary)
    return summary


def enqueue_once(*, poll: bool = False) -> str:
    """Submit a due-date/calendar monitor pass to the shared workflow queue."""
    from app.db.database import SessionLocal
    from app.workflow_service import enqueue_calendar_monitor

    with SessionLocal() as db:
        run = enqueue_calendar_monitor(
            db,
            interval_seconds=int(sync_interval_seconds()),
            poll=poll,
        )
    return run.id


def _loop(stop: threading.Event, sync_interval: float, poll_interval: float) -> None:
    last_poll: float | None = None  # poll on the first pass
    while not stop.is_set():
        started = time.monotonic()
        poll_due = last_poll is None or started - last_poll >= poll_interval
        try:
            enqueue_once(poll=poll_due)
            if poll_due:
                last_poll = started  # measured from the start, so polls stay 5 minutes apart
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            logger.exception("Could not enqueue task calendar monitor pass")
        _wake_event.wait(sync_interval)
        _wake_event.clear()


def start_task_calendar_scheduler() -> bool:
    global _stop_event, _thread
    if not scheduler_enabled():
        logger.info("Task calendar sync disabled (TASK_CALENDAR_SYNC_ENABLED).")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_loop,
        args=(_stop_event, sync_interval_seconds(), poll_interval_seconds()),
        name="task-calendar-sync",
        daemon=True,
    )
    _thread.start()
    logger.info("Task calendar sync started (push every %ss, poll every %ss).",
                sync_interval_seconds(), poll_interval_seconds())
    return True


def stop_task_calendar_scheduler() -> None:
    global _thread
    if _stop_event is not None:
        _stop_event.set()
        wake()
    if _thread is not None:
        _thread.join(timeout=10)
    _thread = None
