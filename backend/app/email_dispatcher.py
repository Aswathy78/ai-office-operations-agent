"""Background dispatcher that delivers queued e-mails and runs due retries (Story 4.4).

Configuration (backend/.env):
- ``EMAIL_DISPATCHER_ENABLED`` (default ``true``)
- ``EMAIL_DISPATCH_INTERVAL_SECONDS`` (default ``2``) — how often the queue is
  checked when nothing wakes it; queueing an e-mail wakes it immediately.

Claims are atomic (see ``email_delivery_service._claim``), so running more than
one process is safe.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_stop_event: threading.Event | None = None
_wake_event = threading.Event()
_thread: threading.Thread | None = None


def dispatcher_enabled() -> bool:
    value = os.getenv("EMAIL_DISPATCHER_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def dispatch_interval_seconds() -> float:
    try:
        return max(0.5, float(os.getenv("EMAIL_DISPATCH_INTERVAL_SECONDS", "2")))
    except ValueError:
        return 2.0


def wake() -> None:
    """Check the queue now instead of waiting for the next interval."""
    _wake_event.set()


def run_once() -> dict:
    from app.db.database import SessionLocal
    from app.email_delivery_service import process_due_emails

    with SessionLocal() as db:
        summary = process_due_emails(db).as_dict()
    if summary["claimed"] or summary["recovered"]:
        logger.info("E-mail dispatch pass: %s", summary)
    return summary


def _loop(stop: threading.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            run_once()
        except Exception:  # noqa: BLE001 - keep the dispatcher alive
            logger.exception("E-mail dispatch pass failed")
        _wake_event.wait(interval)
        _wake_event.clear()


def start_email_dispatcher() -> bool:
    """Start the background thread once. Returns whether a dispatcher is running."""
    global _stop_event, _thread
    if not dispatcher_enabled():
        logger.info("E-mail dispatcher disabled (EMAIL_DISPATCHER_ENABLED).")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_loop,
        args=(_stop_event, dispatch_interval_seconds()),
        name="email-dispatcher",
        daemon=True,
    )
    _thread.start()
    logger.info("E-mail dispatcher started (every %ss).", dispatch_interval_seconds())
    return True


def stop_email_dispatcher() -> None:
    global _thread
    if _stop_event is not None:
        _stop_event.set()
        wake()
    if _thread is not None:
        _thread.join(timeout=10)
    _thread = None
