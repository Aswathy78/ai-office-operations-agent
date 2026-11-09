"""Periodic reminders and expiry processing for assistant approvals (Story 9.7)."""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
_run_lock = threading.Lock()


def scheduler_enabled() -> bool:
    value = os.getenv("ASSISTANT_APPROVAL_SCHEDULER_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def scheduler_interval_seconds() -> int:
    try:
        return max(30, int(os.getenv("ASSISTANT_APPROVAL_SCHEDULER_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


def run_once() -> int:
    """Run one isolated approval maintenance pass."""
    from app.assistant_approvals import process_approval_reminders
    from app.db.database import SessionLocal

    with _run_lock, SessionLocal() as db:
        reminders_sent = process_approval_reminders(db)
    if reminders_sent:
        logger.info("Assistant approval maintenance: %s reminder(s) sent.", reminders_sent)
    return reminders_sent


def _loop(stop: threading.Event, interval: int) -> None:
    if stop.wait(5):
        return
    while not stop.is_set():
        try:
            run_once()
        except Exception:
            logger.exception("Could not process assistant approval reminders and expiry.")
        stop.wait(interval)


def start_assistant_approval_scheduler() -> bool:
    """Start the background approval maintenance thread once."""
    global _stop_event, _thread
    if not scheduler_enabled():
        logger.info("Assistant approval scheduler disabled.")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_loop,
        args=(_stop_event, scheduler_interval_seconds()),
        name="assistant-approval-scheduler",
        daemon=True,
    )
    _thread.start()
    logger.info(
        "Assistant approval scheduler started (every %ss).",
        scheduler_interval_seconds(),
    )
    return True


def stop_assistant_approval_scheduler() -> None:
    global _thread
    if _stop_event is not None:
        _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=10)
    _thread = None
