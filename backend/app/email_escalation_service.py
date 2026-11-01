"""Failure escalation and bounce recording for e-mails sent from chat (Story 4.6 / FR-23).

- Every time an e-mail moves to ``failed`` the requester and the system
  administrators are notified: an e-mail to the requester and to each address
  in ``SYSTEM_ADMIN_EMAILS``, plus an in-app admin record that is always
  written (so nothing is lost if mail itself is down). Notification e-mails go
  through the same providers (with fallback) and are retried on the dispatcher's
  backoff schedule when they cannot be sent.
- Bounces are recorded on the e-mail (``EmailDraft.bounces``): 5xx rejections at
  send time (by ``email_delivery_service``) and bounce reports that arrive later
  (generic JSON or Amazon SES notifications via SNS). A hard bounce on an e-mail
  that was Sent moves it to Failed, so a failed e-mail is never shown as Sent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import smtplib
import uuid

from sqlalchemy import String, cast, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import email_delivery_service as delivery
from app import email_providers
from app.meeting.models import (
    EMAIL_DRAFT_STATUS_FAILED,
    EMAIL_DRAFT_STATUS_SENT,
    EmailDraft,
    EmailDraftEvent,
    EmailFailureNotification,
)

logger = logging.getLogger(__name__)

AUDIENCE_REQUESTER = "requester"
AUDIENCE_ADMIN = "admin"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def admin_emails() -> list[str]:
    raw = os.getenv("SYSTEM_ADMIN_EMAILS", "")
    seen: list[str] = []
    for part in raw.split(","):
        address = part.strip()
        if address and address.casefold() not in {a.casefold() for a in seen}:
            seen.append(address)
    return seen


# ---------------------------------------------------------------------------
# Failure notifications
# ---------------------------------------------------------------------------


def _failure_sequence(db: Session, draft: EmailDraft) -> int | None:
    return (
        db.query(func.max(EmailDraftEvent.sequence))
        .filter(EmailDraftEvent.draft_id == draft.id, EmailDraftEvent.to_status == EMAIL_DRAFT_STATUS_FAILED)
        .scalar()
    )


def _render(draft: EmailDraft, audience: str) -> tuple[str, str]:
    failed = [r for r in draft.delivery_results if r.get("status") != "sent"]
    accepted = [r for r in draft.delivery_results if r.get("status") == "sent"]
    lines = "\n".join(
        f"  - {r['email']}: {r.get('error') or 'not delivered'}" for r in failed
    ) or "  - (see the e-mail record)"
    bounces = "\n".join(
        f"  - {b['recipient']}: {b['type']} bounce ({b['source']}) {b.get('diagnostic') or ''}".rstrip()
        for b in (draft.bounces or [])
    )
    who = "Your e-mail" if audience == AUDIENCE_REQUESTER else f"An e-mail from {draft.created_by_name}"
    subject = f"E-mail delivery failed: {draft.subject}"[:255]
    body = (
        f"{who} could not be delivered to every recipient.\n\n"
        f"Subject: {draft.subject}\n"
        f"Status: Failed after {draft.attempt_count} attempt(s)\n"
        f"Delivered: {len(accepted)} of {len(draft.recipients)} recipient(s)\n\n"
        f"Not delivered:\n{lines}\n"
        + (f"\nBounces:\n{bounces}\n" if bounces else "")
        + f"\nLast error: {draft.last_error or 'unknown'}\n\n"
        + ("Open the AI Assistant to review the e-mail and retry it.\n"
           if audience == AUDIENCE_REQUESTER
           else "See the Dashboard's e-mail delivery panel for details.\n")
        + "\nThis message was sent automatically by the Office Operations system.\n"
    )
    return subject, body


def notify_failure(db: Session, draft: EmailDraft) -> list[EmailFailureNotification]:
    """Record and send the failure notifications for the e-mail's latest Failed event (idempotent)."""
    sequence = _failure_sequence(db, draft)
    if sequence is None:
        return []
    existing = (
        db.query(EmailFailureNotification)
        .filter(EmailFailureNotification.draft_id == draft.id,
                EmailFailureNotification.failure_event_sequence == sequence)
        .all()
    )
    if existing:
        from app.notification_service import record_email_draft_failure

        record_email_draft_failure(db, draft, existing)
        return existing

    now = _now()
    rows: list[EmailFailureNotification] = []

    def add(audience: str, channel: str, address: str | None) -> None:
        subject, body = _render(draft, audience)
        rows.append(EmailFailureNotification(
            id=str(uuid.uuid4()),
            draft_id=draft.id,
            failure_event_sequence=sequence,
            audience=audience,
            channel=channel,
            recipient_email=address,
            subject=subject,
            body=body,
            idempotency_key=f"email-failure:{draft.id}:{sequence}:{audience}:{channel}:{(address or '').casefold()}",
            status="pending" if channel == "email" else "recorded",
            attempts=0,
            created_at=now,
        ))

    requester = (draft.created_by_email or "").strip()
    if requester:
        add(AUDIENCE_REQUESTER, "email", requester)
    for address in admin_emails():
        if address.casefold() != requester.casefold():
            add(AUDIENCE_ADMIN, "email", address)
    add(AUDIENCE_ADMIN, "in_app", None)  # always visible on the admin dashboard

    draft.escalated_at = now
    db.add_all(rows)
    try:
        db.commit()
    except IntegrityError:  # another process recorded them first
        db.rollback()
        return (
            db.query(EmailFailureNotification)
            .filter(EmailFailureNotification.draft_id == draft.id,
                    EmailFailureNotification.failure_event_sequence == sequence)
            .all()
        )
    if not requester:
        logger.warning("Failed e-mail %s has no requester address; admins notified only", draft.id)

    for row in rows:
        if row.channel == "email":
            _send_notification(db, row, now)
    from app.notification_service import record_email_draft_failure

    record_email_draft_failure(db, draft, rows)
    return rows


def _send_notification(db: Session, row: EmailFailureNotification, now: datetime) -> None:
    from app.notification_service import email_delivery_policy

    policy, next_attempt_at, policy_error = email_delivery_policy(
        db,
        row.recipient_email or "",
        now,
    )
    if policy == "disabled":
        row.status = "recorded"
        row.error = policy_error
        row.next_attempt_at = None
        db.commit()
        return
    if policy == "deferred":
        row.status = "pending"
        row.error = policy_error
        row.next_attempt_at = next_attempt_at
        db.commit()
        return
    row.attempts += 1
    try:
        receipt = email_providers.send_email(row.recipient_email, row.subject, row.body)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        row.next_attempt_at = (
            now + timedelta(seconds=delivery.retry_backoff_seconds(row.attempts))
            if row.attempts < delivery.max_attempts()
            else None
        )
        logger.warning("Failure notification not sent draft_id=%s to=%s: %s",
                       row.draft_id, row.recipient_email, exc)
    else:
        row.status = "sent"
        row.provider = receipt.provider
        row.error = None
        row.next_attempt_at = None
        row.sent_at = _now()
    db.commit()


def retry_due_notifications(db: Session, now: datetime | None = None) -> int:
    """Retry failure notifications whose earlier attempt failed (dispatcher pass)."""
    now = now or _now()
    stale_pending = _now() - timedelta(minutes=5)
    due = (
        db.query(EmailFailureNotification)
        .filter(EmailFailureNotification.channel == "email")
        .filter(
            ((EmailFailureNotification.status == "failed")
             & EmailFailureNotification.next_attempt_at.isnot(None)
             & (EmailFailureNotification.next_attempt_at <= now))
            | ((EmailFailureNotification.status == "pending")
                & (EmailFailureNotification.created_at < stale_pending)
                & ((EmailFailureNotification.next_attempt_at.is_(None))
                   | (EmailFailureNotification.next_attempt_at <= now)))
        )
        .limit(20)
        .all()
    )
    for row in due:
        _send_notification(db, row, now)
        from app.notification_service import sync_email_failure_notification

        sync_email_failure_notification(db, row.id)
    return len(due)


def list_notifications(db: Session, draft_id: str) -> list[EmailFailureNotification]:
    return (
        db.query(EmailFailureNotification)
        .filter(EmailFailureNotification.draft_id == draft_id)
        .order_by(EmailFailureNotification.created_at, EmailFailureNotification.audience)
        .all()
    )


# ---------------------------------------------------------------------------
# Bounces
# ---------------------------------------------------------------------------


def _core_id(message_id: str) -> str:
    return message_id.strip().strip("<>").strip()


def find_draft_by_message_id(db: Session, message_id: str) -> tuple[EmailDraft, dict] | None:
    core = _core_id(message_id)
    if not core:
        return None
    candidates = (
        db.query(EmailDraft)
        .filter(cast(EmailDraft.delivery_results, String).like(f"%{core}%"))
        .all()
    )
    for draft in candidates:
        for result in draft.delivery_results or []:
            if result.get("message_id") and _core_id(result["message_id"]) == core:
                return draft, result
    return None


def record_bounce(
    db: Session,
    *,
    message_id: str,
    recipient: str | None,
    bounce_type: str,
    diagnostic: str | None = None,
    code: int | None = None,
    source: str = "dsn",
) -> EmailDraft | None:
    """Attach a bounce report to the e-mail it belongs to. Returns None if unknown."""
    if bounce_type not in ("hard", "soft"):
        raise ValueError("bounce_type must be 'hard' or 'soft'.")
    found = find_draft_by_message_id(db, message_id)
    if found is None:
        return None
    draft, result = found
    recipient = (recipient or result["email"]).strip()

    duplicate = any(
        b.get("message_id") and _core_id(b["message_id"]) == _core_id(message_id)
        and b["recipient"].casefold() == recipient.casefold() and b["type"] == bounce_type
        for b in draft.bounces or []
    )
    if duplicate:
        return draft

    delivery._add_bounce(draft, recipient=recipient, bounce_type=bounce_type, source=source,
                         code=code, diagnostic=diagnostic, message_id=message_id)
    if bounce_type == "hard":
        # The provider accepted it, but it never arrived: this recipient is not delivered.
        draft.delivery_results = [
            {**r, "status": "bounced", "error": (diagnostic or "Hard bounce")[:500]}
            if r is result else r
            for r in draft.delivery_results
        ]
        bounced = sum(1 for r in draft.delivery_results if r.get("status") == "bounced")
        draft.last_error = (
            f"{bounced} of {len(draft.recipients)} recipient(s) bounced. "
            f"{recipient}: {diagnostic or 'hard bounce'}"
        )[:1000]
        if draft.status == EMAIL_DRAFT_STATUS_SENT:
            delivery.transition(
                db, draft, EMAIL_DRAFT_STATUS_FAILED, actor_id=delivery.SYSTEM_ACTOR,
                detail=f"Hard bounce reported for {recipient} ({source}). {diagnostic or ''}".strip()[:1000],
            )
    draft.updated_at = _now()
    db.commit()
    if bounce_type == "hard" and draft.status == EMAIL_DRAFT_STATUS_FAILED:
        delivery.escalate(db, draft)
    db.refresh(draft)
    return draft


class SubscriptionConfirmation(Exception):
    """An SNS subscription handshake, not a bounce."""

    def __init__(self, subscribe_url: str | None):
        super().__init__("SNS subscription confirmation")
        self.subscribe_url = subscribe_url


def parse_bounce_payload(payload: dict) -> list[dict]:
    """Normalise a bounce report into ``[{message_id, recipient, bounce_type, diagnostic, code, source}]``.

    Accepts the generic format ``{"message_id", "recipient", "bounce_type", "diagnostic", "code"}``,
    an Amazon SES bounce notification, or the SNS envelope that wraps one.
    """
    if payload.get("Type") == "SubscriptionConfirmation":
        raise SubscriptionConfirmation(payload.get("SubscribeURL"))
    if payload.get("Type") == "Notification" and isinstance(payload.get("Message"), str):
        try:
            payload = json.loads(payload["Message"])
        except json.JSONDecodeError as exc:
            raise ValueError("SNS message is not valid JSON.") from exc

    if payload.get("notificationType") == "Bounce" or "bounce" in payload and "mail" in payload:
        bounce = payload.get("bounce") or {}
        mail = payload.get("mail") or {}
        message_id = (mail.get("commonHeaders") or {}).get("messageId") or next(
            (h.get("value") for h in mail.get("headers") or [] if (h.get("name") or "").lower() == "message-id"),
            None,
        ) or mail.get("messageId")
        bounce_type = "hard" if bounce.get("bounceType") == "Permanent" else "soft"
        reports = []
        for recipient in bounce.get("bouncedRecipients") or []:
            status = recipient.get("status") or ""
            code = None
            if status[:1].isdigit():
                code = {"5": 550, "4": 450}.get(status[0])
            reports.append({
                "message_id": message_id,
                "recipient": recipient.get("emailAddress"),
                "bounce_type": bounce_type,
                "diagnostic": recipient.get("diagnosticCode") or bounce.get("bounceSubType"),
                "code": code,
                "source": "ses",
            })
        return reports

    if not payload.get("message_id"):
        raise ValueError("A bounce needs a message_id.")
    bounce_type = (payload.get("bounce_type") or "hard").lower()
    return [{
        "message_id": payload["message_id"],
        "recipient": payload.get("recipient"),
        "bounce_type": bounce_type,
        "diagnostic": payload.get("diagnostic"),
        "code": payload.get("code"),
        "source": payload.get("source") or "dsn",
    }]
