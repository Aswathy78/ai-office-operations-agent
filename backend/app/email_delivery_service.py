"""E-mail delivery lifecycle (Story 4.4 / FR-22).

Every e-mail drafted from chat has exactly one status from this lifecycle::

    draft ──submit──> approval ──decision / edit──> draft
    draft ──send──> queued ──dispatcher claims──> sending
    queued ──cancel──> draft
    sending ──provider accepted every recipient──> sent
    sending ──temporary failure, attempts left──> retry ──backoff elapsed──> sending
    sending ──permanent failure or attempts used up──> failed ──user retries──> retry
    sent ──hard bounce reported later──> failed

Story 4.6 / FR-23 additions: transient errors are retried after 1, 5 and 15
minutes (then every 15) for up to 5 attempts; the mail provider falls back to
``FALLBACK_SMTP_*`` (e.g. SES) when the primary is unavailable; 5xx rejections
and later bounce reports are recorded in ``EmailDraft.bounces``; and every move
to ``failed`` notifies the requester and the administrators
(``app.email_escalation_service``). A failed e-mail is never shown as sent.

``sent`` is only set after the SMTP provider has accepted the message for every
recipient (``task_service.deliver_email`` returned a Message-ID). Each
recipient's acceptance is stored as soon as it happens, so a retry never
re-sends to someone who already received the e-mail.

Every status change goes through :func:`transition`, which rejects moves not in
``ALLOWED_TRANSITIONS`` and records an :class:`EmailDraftEvent`. The dispatcher
claims work with a conditional UPDATE, so two processes can never send the same
e-mail at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import os
import smtplib
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import email_providers
from app.meeting.models import (
    EMAIL_DRAFT_STATUS_APPROVAL,
    EMAIL_DRAFT_STATUS_DRAFT,
    EMAIL_DRAFT_STATUS_FAILED,
    EMAIL_DRAFT_STATUS_QUEUED,
    EMAIL_DRAFT_STATUS_RETRY,
    EMAIL_DRAFT_STATUS_SENDING,
    EMAIL_DRAFT_STATUS_SENT,
    EmailDraft,
    EmailDraftEvent,
)

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system"

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    EMAIL_DRAFT_STATUS_DRAFT: frozenset({EMAIL_DRAFT_STATUS_APPROVAL, EMAIL_DRAFT_STATUS_QUEUED}),
    EMAIL_DRAFT_STATUS_APPROVAL: frozenset({EMAIL_DRAFT_STATUS_DRAFT}),
    EMAIL_DRAFT_STATUS_QUEUED: frozenset({EMAIL_DRAFT_STATUS_SENDING, EMAIL_DRAFT_STATUS_DRAFT}),
    EMAIL_DRAFT_STATUS_SENDING: frozenset({
        EMAIL_DRAFT_STATUS_SENT,
        EMAIL_DRAFT_STATUS_FAILED,
        EMAIL_DRAFT_STATUS_RETRY,
    }),
    EMAIL_DRAFT_STATUS_RETRY: frozenset({EMAIL_DRAFT_STATUS_SENDING}),
    EMAIL_DRAFT_STATUS_FAILED: frozenset({EMAIL_DRAFT_STATUS_RETRY}),
    # Only a hard bounce reported after acceptance moves a sent e-mail (to failed).
    EMAIL_DRAFT_STATUS_SENT: frozenset({EMAIL_DRAFT_STATUS_FAILED}),
}

# Statuses that will change without the sender doing anything; the UI polls these.
IN_FLIGHT_STATUSES = frozenset({
    EMAIL_DRAFT_STATUS_APPROVAL,
    EMAIL_DRAFT_STATUS_QUEUED,
    EMAIL_DRAFT_STATUS_SENDING,
    EMAIL_DRAFT_STATUS_RETRY,
})
ATTENTION_STATUSES = (EMAIL_DRAFT_STATUS_FAILED, EMAIL_DRAFT_STATUS_RETRY)


class InvalidEmailTransition(Exception):
    """The requested status change is not part of the lifecycle (HTTP 409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_MINUTES = (1, 5, 15)


def max_attempts() -> int:
    try:
        return max(1, int(os.getenv("EMAIL_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def backoff_schedule_minutes() -> tuple[float, ...]:
    raw = os.getenv("EMAIL_RETRY_BACKOFF_MINUTES", "")
    try:
        schedule = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError:
        schedule = ()
    return tuple(m for m in schedule if m > 0) or DEFAULT_BACKOFF_MINUTES


def retry_backoff_seconds(attempt: int) -> int:
    """Delay after failed attempt ``attempt``: 1, 5, 15 minutes, then 15 for every later retry."""
    schedule = backoff_schedule_minutes()
    return int(schedule[min(max(attempt, 1), len(schedule)) - 1] * 60)


def stuck_sending_seconds() -> int:
    try:
        return max(30, int(os.getenv("EMAIL_STUCK_SENDING_SECONDS", "300")))
    except ValueError:
        return 300


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def _record_event(
    db: Session,
    draft: EmailDraft,
    from_status: str | None,
    to_status: str,
    actor_id: str,
    detail: str | None,
    at: datetime,
) -> None:
    db.flush()  # include events added earlier in this transaction
    last = (
        db.query(func.max(EmailDraftEvent.sequence))
        .filter(EmailDraftEvent.draft_id == draft.id)
        .scalar()
    )
    db.add(EmailDraftEvent(
        id=str(uuid.uuid4()),
        draft_id=draft.id,
        sequence=(last or 0) + 1,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_id,
        detail=detail,
        created_at=at,
    ))


def transition(
    db: Session,
    draft: EmailDraft,
    to_status: str,
    *,
    actor_id: str,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """Move ``draft`` to ``to_status`` (caller commits). Rejects moves outside the lifecycle."""
    from_status = draft.status
    if not can_transition(from_status, to_status):
        raise InvalidEmailTransition(
            f"An e-mail cannot move from {from_status} to {to_status}."
        )
    at = now or _now()
    draft.status = to_status
    draft.status_changed_at = at
    draft.updated_at = at
    _record_event(db, draft, from_status, to_status, actor_id, detail, at)


def record_created(db: Session, draft: EmailDraft, actor_id: str) -> None:
    """History entry for a new draft (caller commits)."""
    at = draft.created_at or _now()
    draft.status_changed_at = at
    _record_event(db, draft, None, draft.status, actor_id, "Drafted from chat.", at)


def list_events(db: Session, draft_id: str) -> list[EmailDraftEvent]:
    return (
        db.query(EmailDraftEvent)
        .filter(EmailDraftEvent.draft_id == draft_id)
        .order_by(EmailDraftEvent.sequence)
        .all()
    )


# ---------------------------------------------------------------------------
# Sender actions
# ---------------------------------------------------------------------------


def queue_for_delivery(
    db: Session,
    draft: EmailDraft,
    *,
    actor_id: str,
    acknowledged_sensitive: bool,
    now: datetime | None = None,
) -> None:
    """Draft -> Queued. The dispatcher picks it up within a second or two."""
    at = now or _now()
    transition(db, draft, EMAIL_DRAFT_STATUS_QUEUED, actor_id=actor_id,
               detail="Sender confirmed the preview.", now=at)
    draft.acknowledged_sensitive = acknowledged_sensitive
    draft.queued_at = at
    draft.attempt_count = 0
    draft.next_attempt_at = at
    draft.last_error = None
    db.commit()
    wake_dispatcher()


def cancel_queued(db: Session, draft: EmailDraft, *, actor_id: str) -> bool:
    """Queued -> Draft, only if the dispatcher has not claimed it yet."""
    at = _now()
    claimed = (
        db.query(EmailDraft)
        .filter(EmailDraft.id == draft.id, EmailDraft.status == EMAIL_DRAFT_STATUS_QUEUED)
        .update(
            {
                EmailDraft.status: EMAIL_DRAFT_STATUS_DRAFT,
                EmailDraft.status_changed_at: at,
                EmailDraft.updated_at: at,
                EmailDraft.next_attempt_at: None,
            },
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.rollback()
        return False
    _record_event(db, draft, EMAIL_DRAFT_STATUS_QUEUED, EMAIL_DRAFT_STATUS_DRAFT, actor_id,
                  "Sending cancelled by the sender.", at)
    db.commit()
    db.refresh(draft)
    return True


def request_retry(db: Session, draft: EmailDraft, *, actor_id: str) -> None:
    """Failed -> Retry: one more attempt for the recipients that did not get it."""
    at = _now()
    transition(db, draft, EMAIL_DRAFT_STATUS_RETRY, actor_id=actor_id,
               detail="Sender asked to retry undelivered recipients.", now=at)
    draft.next_attempt_at = at
    db.commit()
    wake_dispatcher()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class DispatchSummary:
    claimed: int = 0
    sent: int = 0
    retry: int = 0
    failed: int = 0
    recovered: int = 0
    notifications_retried: int = 0
    draft_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "retry": self.retry,
            "failed": self.failed,
            "recovered": self.recovered,
            "notifications_retried": self.notifications_retried,
            "draft_ids": list(self.draft_ids),
        }


def _claim(db: Session, draft_id: str, from_status: str, now: datetime | None = None) -> bool:
    """Atomically move a due e-mail to Sending; False if another worker got it first."""
    now = _now()
    claimed = (
        db.query(EmailDraft)
        .filter(EmailDraft.id == draft_id, EmailDraft.status == from_status)
        .update(
            {
                EmailDraft.status: EMAIL_DRAFT_STATUS_SENDING,
                EmailDraft.status_changed_at: now,
                EmailDraft.updated_at: now,
                EmailDraft.attempt_count: EmailDraft.attempt_count + 1,
            },
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.rollback()
        return False
    draft = db.get(EmailDraft, draft_id)
    db.refresh(draft)
    _record_event(db, draft, from_status, EMAIL_DRAFT_STATUS_SENDING, SYSTEM_ACTOR,
                  f"Delivery attempt {draft.attempt_count} started.", now)
    db.commit()
    return True


def is_transient(error: Exception) -> bool:
    """Whether trying again later could succeed (network trouble, 4xx replies)."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in error.recipients.values()]
        return bool(codes) and all(400 <= code < 500 for code in codes)
    if isinstance(error, smtplib.SMTPResponseException):
        return 400 <= error.smtp_code < 500
    if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(error, ValueError):
        return False  # configuration problems will not fix themselves
    return isinstance(error, OSError)


def _describe(error: Exception) -> str:
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        parts = [f"{code} {msg.decode(errors='replace') if isinstance(msg, bytes) else msg}"
                 for code, msg in error.recipients.values()]
        return "Recipient refused by the mail server: " + "; ".join(parts)
    if isinstance(error, smtplib.SMTPResponseException):
        message = error.smtp_error
        if isinstance(message, bytes):
            message = message.decode(errors="replace")
        return f"Mail server replied {error.smtp_code}: {message}"
    return str(error) or type(error).__name__


def _human_delay(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds}s"


def _add_bounce(
    draft: EmailDraft,
    *,
    recipient: str,
    bounce_type: str,
    source: str,
    code: int | None,
    diagnostic: str | None,
    message_id: str | None,
) -> dict:
    entry = {
        "recipient": recipient,
        "type": bounce_type,
        "source": source,
        "code": code,
        "diagnostic": (diagnostic or "")[:500] or None,
        "message_id": message_id,
        "recorded_at": _now().isoformat(),
    }
    draft.bounces = [*(draft.bounces or []), entry]
    return entry


def escalate(db: Session, draft: EmailDraft) -> None:
    """Tell the requester and administrators that ``draft`` failed (never raises)."""
    from app.email_escalation_service import notify_failure

    try:
        notify_failure(db, draft)
    except Exception:  # noqa: BLE001 - the failure itself is already recorded
        db.rollback()
        logger.exception("Could not record failure notifications draft_id=%s", draft.id)


def _retry_notifications(db: Session, now: datetime) -> int:
    from app.email_escalation_service import retry_due_notifications

    try:
        return retry_due_notifications(db, now)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Retrying failure notifications failed")
        return 0


def deliver(db: Session, draft: EmailDraft, now: datetime) -> str:
    """Send to every recipient not yet accepted; returns the resulting status.

    ``now`` is the dispatcher's clock (backoff is scheduled from it); status
    and event timestamps always use the real time.
    """
    accepted = {
        r["email"].casefold() for r in draft.delivery_results if r.get("status") == "sent"
    }
    results = [r for r in draft.delivery_results if r["email"].casefold() in accepted]
    errors: list[Exception] = []

    for recipient in draft.recipients:
        address = recipient["email"]
        if address.casefold() in accepted:
            continue
        try:
            receipt = email_providers.send_email(address, draft.subject, draft.body)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            errors.append(exc)
            logger.warning("E-mail delivery failed draft_id=%s recipient=%s: %s",
                           draft.id, address, exc)
            results.append({
                "email": address, "status": "failed", "error": _describe(exc)[:500],
                "message_id": None, "accepted_at": None, "attempt": draft.attempt_count,
                "provider": None, "fallback_used": False,
            })
            code = email_providers.smtp_code(exc)
            if code is not None and code >= 500:
                _add_bounce(draft, recipient=address, bounce_type="hard", source="smtp",
                            code=code, diagnostic=_describe(exc), message_id=None)
        else:
            results.append({
                "email": address, "status": "sent", "error": None,
                "message_id": receipt.message_id, "accepted_at": _now().isoformat(),
                "attempt": draft.attempt_count, "provider": receipt.provider,
                "fallback_used": receipt.fallback_used,
            })
        # Persist each acceptance immediately so a crash never causes a re-send.
        draft.delivery_results = [dict(r) for r in results]
        db.commit()

    draft.delivery_results = results
    total = len(draft.recipients)
    delivered = sum(1 for r in results if r["status"] == "sent")
    finished = _now()

    if not errors:
        draft.sent_at = finished
        draft.last_error = None
        draft.next_attempt_at = None
        transition(db, draft, EMAIL_DRAFT_STATUS_SENT, actor_id=SYSTEM_ACTOR,
                   detail=f"Accepted by the mail provider for all {total} recipient(s).",
                   now=finished)
    else:
        failed_count = total - delivered
        draft.last_error = (
            f"{failed_count} of {total} recipient(s) not delivered. " + _describe(errors[-1])
        )[:1000]
        if delivered and draft.sent_at is None:
            draft.sent_at = finished
        if all(is_transient(error) for error in errors) and draft.attempt_count < max_attempts():
            delay = retry_backoff_seconds(draft.attempt_count)
            draft.next_attempt_at = max(finished, _as_utc(now)) + timedelta(seconds=delay)
            transition(db, draft, EMAIL_DRAFT_STATUS_RETRY, actor_id=SYSTEM_ACTOR,
                       detail=f"Attempt {draft.attempt_count} of {max_attempts()} failed "
                              f"({failed_count} recipient(s)); retrying in {_human_delay(delay)}. "
                              f"{_describe(errors[-1])}"[:1000],
                       now=finished)
        else:
            draft.next_attempt_at = None
            reason = ("no attempts left" if all(is_transient(e) for e in errors)
                      else "the error is permanent")
            transition(db, draft, EMAIL_DRAFT_STATUS_FAILED, actor_id=SYSTEM_ACTOR,
                       detail=f"Attempt {draft.attempt_count} failed and {reason}. "
                              f"{_describe(errors[-1])}"[:1000],
                       now=finished)
    db.commit()
    if draft.status == EMAIL_DRAFT_STATUS_FAILED:
        escalate(db, draft)
    return draft.status


def recover_stuck_sending(db: Session, now: datetime | None = None) -> int:
    """E-mails left in Sending by a crashed process are marked Failed (never re-sent blindly)."""
    now = now or _now()
    cutoff = now - timedelta(seconds=stuck_sending_seconds())
    stuck = (
        db.query(EmailDraft)
        .filter(EmailDraft.status == EMAIL_DRAFT_STATUS_SENDING)
        .filter(EmailDraft.status_changed_at < cutoff)
        .all()
    )
    for draft in stuck:
        draft.last_error = ("Delivery was interrupted. Recipients marked as delivered were "
                            "accepted by the mail provider; retry to send to the rest.")
        draft.next_attempt_at = None
        transition(db, draft, EMAIL_DRAFT_STATUS_FAILED, actor_id=SYSTEM_ACTOR,
                   detail="Interrupted while sending.", now=now)
    if stuck:
        db.commit()
        for draft in stuck:
            escalate(db, draft)
    return len(stuck)


def process_due_emails(db: Session, now: datetime | None = None, limit: int = 20) -> DispatchSummary:
    """One dispatcher pass: claim queued / due-retry e-mails and deliver them."""
    now = _as_utc(now or _now())
    summary = DispatchSummary(recovered=recover_stuck_sending(db, now))
    due = (
        db.query(EmailDraft.id, EmailDraft.status)
        .filter(EmailDraft.status.in_((EMAIL_DRAFT_STATUS_QUEUED, EMAIL_DRAFT_STATUS_RETRY)))
        .filter((EmailDraft.next_attempt_at.is_(None)) | (EmailDraft.next_attempt_at <= now))
        .order_by(EmailDraft.next_attempt_at)
        .limit(limit)
        .all()
    )
    for draft_id, status in due:
        if not _claim(db, draft_id, status, now):
            continue
        summary.claimed += 1
        summary.draft_ids.append(draft_id)
        draft = db.get(EmailDraft, draft_id)
        try:
            outcome = deliver(db, draft, now)
        except Exception as exc:  # noqa: BLE001 - never leave an e-mail stuck in Sending
            db.rollback()
            draft = db.get(EmailDraft, draft_id)
            db.refresh(draft)
            logger.exception("E-mail dispatch crashed draft_id=%s", draft_id)
            if not can_transition(draft.status, EMAIL_DRAFT_STATUS_FAILED):
                # Someone else already moved it (or the claim was lost); leave it as is.
                logger.warning("Not marking draft_id=%s failed from status %s", draft_id, draft.status)
                continue
            draft.last_error = f"Unexpected error while sending: {exc}"[:1000]
            draft.next_attempt_at = None
            transition(db, draft, EMAIL_DRAFT_STATUS_FAILED, actor_id=SYSTEM_ACTOR,
                       detail="Unexpected error while sending.")
            db.commit()
            escalate(db, draft)
            outcome = EMAIL_DRAFT_STATUS_FAILED
        if outcome == EMAIL_DRAFT_STATUS_SENT:
            summary.sent += 1
        elif outcome == EMAIL_DRAFT_STATUS_RETRY:
            summary.retry += 1
        else:
            summary.failed += 1
    summary.notifications_retried = _retry_notifications(db, now)
    return summary


def wake_dispatcher() -> None:
    """Ask the background dispatcher (if running) to check the queue now."""
    from app.email_dispatcher import wake

    wake()
