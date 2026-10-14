"""Meeting minutes aggregation, snapshot persistence, and recipient selection service."""

from datetime import datetime, timezone
import logging
import re
import smtplib
import uuid

from sqlalchemy.orm import Session

from app.meeting.models import (
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingAttendanceLog,
    MeetingMinutes,
    MeetingMinutesRecipient,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    MINUTES_DELIVERY_STATUS_FAILED,
    MINUTES_DELIVERY_STATUS_PENDING,
    MINUTES_DELIVERY_STATUS_SENT,
    MINUTES_POLICY_MANUAL_APPROVAL,
    MINUTES_STATUS_APPROVED,
    MINUTES_STATUS_PENDING_APPROVAL,
    MINUTES_STATUS_SENT,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_recipient_email(email: str | None) -> bool:
    """Validate that the recipient email is non-empty and well-formed."""
    if not email or not isinstance(email, str):
        return False
    cleaned = email.strip()
    if not cleaned or len(cleaned) > 255:
        return False
    return bool(EMAIL_REGEX.match(cleaned))


def get_eligible_attendance_recipients(
    db: Session,
    meeting_id: str,
) -> list[tuple[str, str, str | None]]:
    """Retrieve distinct attendees who actually joined the meeting with valid emails.

    Returns a list of tuples: (recipient_name, recipient_email, attendance_log_id).
    Scheduled invitees who did not attend are excluded.
    Deduplication is performed case-insensitively based on recipient_email.
    """
    attendance_records = (
        db.query(MeetingAttendanceLog)
        .filter(MeetingAttendanceLog.meeting_id == meeting_id)
        .order_by(MeetingAttendanceLog.joined_at.asc(), MeetingAttendanceLog.id.asc())
        .all()
    )

    seen_emails: set[str] = set()
    recipients: list[tuple[str, str, str | None]] = []

    for record in attendance_records:
        raw_email = (record.participant_email or "").strip()
        if not raw_email and record.user_id:
            emp = db.query(Employee).filter(Employee.id == record.user_id).first()
            if emp and emp.email:
                raw_email = emp.email.strip()
                if not record.participant_name and emp.name:
                    record.participant_name = emp.name

        if not is_valid_recipient_email(raw_email):
            continue

        normalized_email = raw_email.lower()
        if normalized_email in seen_emails:
            continue

        seen_emails.add(normalized_email)
        name = (record.participant_name or "").strip() or raw_email
        recipients.append((name, raw_email, record.id))

    return recipients


def aggregate_minutes_content(
    db: Session,
    meeting_id: str,
) -> tuple[str | None, list[str], list[dict[str, object]], str | None]:
    """Aggregate the latest completed summary, decisions, confirmed action items, and resolved dates.

    Does not invent missing dates or decisions.
    """
    summary = (
        db.query(MeetingSummary)
        .filter(
            MeetingSummary.meeting_id == meeting_id,
            MeetingSummary.status == SUMMARY_STATUS_COMPLETED,
        )
        .order_by(MeetingSummary.created_at.desc(), MeetingSummary.id.desc())
        .first()
    )

    if summary is None:
        rec = (
            db.query(MeetingRecording)
            .filter(MeetingRecording.meeting_id == meeting_id)
            .first()
        )
        if rec is not None:
            transcript = (
                db.query(MeetingTranscript)
                .filter(
                    MeetingTranscript.recording_id == rec.id,
                    MeetingTranscript.status == TRANSCRIPT_STATUS_COMPLETED,
                )
                .first()
            )
            if (
                transcript is not None
                and transcript.transcript_text
                and transcript.transcript_text.strip()
            ):
                try:
                    from app.meeting.summary_service import generate_meeting_summary

                    summary = generate_meeting_summary(db, transcript.id)
                except Exception:
                    logger.debug("Automatic summary generation skipped for %s", meeting_id)

    content_summary = summary.overview if (summary and summary.overview) else None
    content_decisions = list(summary.decisions or []) if (summary and summary.decisions) else []

    confirmed_items = (
        db.query(MeetingActionItem)
        .join(
            MeetingActionItemExtraction,
            MeetingActionItem.extraction_id == MeetingActionItemExtraction.id,
        )
        .filter(
            MeetingActionItemExtraction.meeting_id == meeting_id,
            MeetingActionItem.review_status == ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
        )
        .order_by(
            MeetingActionItem.source_segment_index.asc(),
            MeetingActionItem.id.asc(),
        )
        .all()
    )

    content_action_items: list[dict[str, object]] = []
    for item in confirmed_items:
        owner_name = None
        owner_email = None
        if item.confirmed_employee:
            owner_name = item.confirmed_employee.name
            owner_email = item.confirmed_employee.email
        elif item.owner_name:
            owner_name = item.owner_name

        if item.deadline_at is not None:
            dl = item.deadline_at
            if dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
            resolved_deadline_at: str | None = dl.isoformat()
        else:
            resolved_deadline_at = None

        content_action_items.append(
            {
                "id": item.id,
                "task_description": item.task_description,
                "owner_name": owner_name,
                "owner_email": owner_email,
                "deadline_at": resolved_deadline_at,
                "deadline_text": item.deadline_text,
                "priority": item.priority,
            }
        )

    summary_id = summary.id if summary else None
    return content_summary, content_decisions, content_action_items, summary_id


def _populate_minutes_recipients(
    db: Session,
    minutes: MeetingMinutes,
) -> list[MeetingMinutesRecipient]:
    """Populate missing attendee recipients for a minutes record."""
    existing_recipients = {
        rec.recipient_email.strip().lower() for rec in minutes.recipients
    }

    eligible = get_eligible_attendance_recipients(db, minutes.meeting_id)
    added: list[MeetingMinutesRecipient] = []

    for name, email, attendance_log_id in eligible:
        norm_email = email.strip().lower()
        if norm_email in existing_recipients:
            continue

        idempotency_key = f"minutes:{minutes.id}:{norm_email}"
        recipient = MeetingMinutesRecipient(
            id=str(uuid.uuid4()),
            minutes_id=minutes.id,
            recipient_name=name,
            recipient_email=email,
            attendance_log_id=attendance_log_id,
            delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
            idempotency_key=idempotency_key,
        )
        db.add(recipient)
        existing_recipients.add(norm_email)
        added.append(recipient)

    return added


def refresh_minutes_content(
    db: Session,
    minutes: MeetingMinutes,
) -> MeetingMinutes:
    """Re-aggregate and update content for minutes that have not been sent yet.

    If minutes were previously approved, resets them to 'pending_approval' so the
    organizer can review and confirm the refreshed content before delivery.
    Minutes in 'sent' status are locked and cannot be refreshed.
    """
    if minutes.status == MINUTES_STATUS_SENT:
        return minutes

    (
        content_summary,
        content_decisions,
        content_action_items,
        summary_id,
    ) = aggregate_minutes_content(db, minutes.meeting_id)

    minutes.content_summary = content_summary
    minutes.content_decisions = content_decisions
    minutes.content_action_items = content_action_items
    if summary_id:
        minutes.summary_id = summary_id

    if minutes.status == MINUTES_STATUS_APPROVED:
        minutes.status = MINUTES_STATUS_PENDING_APPROVAL
        minutes.approved_at = None
        minutes.approved_by = None

    minutes.updated_at = datetime.now(timezone.utc)
    _populate_minutes_recipients(db, minutes)
    db.commit()
    db.refresh(minutes)
    return minutes


def update_meeting_minutes_content(
    db: Session,
    meeting_id: str,
    *,
    content_summary: str | None = None,
    content_decisions: list[str] | None = None,
    content_action_items: list[dict[str, object]] | None = None,
) -> MeetingMinutes:
    """Allow organizer to edit draft or pending meeting minutes content.

    If minutes were previously approved, resets them to 'pending_approval' so
    the organizer can explicitly re-confirm and re-approve before delivery.
    Cannot edit minutes that have already been sent.
    """
    minutes = get_meeting_minutes(db, meeting_id)
    if minutes is None:
        raise ValueError("Meeting minutes not found.")
    if minutes.status == MINUTES_STATUS_SENT:
        raise ValueError("Cannot edit minutes that have already been sent to attendees.")

    if content_summary is not None:
        minutes.content_summary = content_summary.strip() if content_summary.strip() else None
    if content_decisions is not None:
        minutes.content_decisions = [d.strip() for d in content_decisions if d and d.strip()]
    if content_action_items is not None:
        minutes.content_action_items = content_action_items

    if minutes.status == MINUTES_STATUS_APPROVED:
        minutes.status = MINUTES_STATUS_PENDING_APPROVAL
        minutes.approved_at = None
        minutes.approved_by = None

    minutes.updated_at = datetime.now(timezone.utc)
    _populate_minutes_recipients(db, minutes)
    db.commit()
    db.refresh(minutes)
    return minutes


def generate_or_get_meeting_minutes(
    db: Session,
    meeting_id: str,
    *,
    force_refresh: bool = False,
) -> MeetingMinutes:
    """Generate a stable minutes snapshot for a meeting or return the existing one.

    When ``force_refresh=True`` and minutes are not yet approved, re-aggregates
    content so that newly confirmed action items and summaries are included.
    Defaults to manual organizer approval policy.
    Does not approve or dispatch emails automatically.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError("Meeting not found.")

    existing_minutes = (
        db.query(MeetingMinutes)
        .filter(MeetingMinutes.meeting_id == meeting_id)
        .order_by(MeetingMinutes.created_at.desc(), MeetingMinutes.id.desc())
        .first()
    )

    if existing_minutes is not None:
        if force_refresh:
            return refresh_minutes_content(db, existing_minutes)
        _populate_minutes_recipients(db, existing_minutes)
        db.commit()
        db.refresh(existing_minutes)
        return existing_minutes

    (
        content_summary,
        content_decisions,
        content_action_items,
        summary_id,
    ) = aggregate_minutes_content(db, meeting_id)

    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        summary_id=summary_id,
        status=MINUTES_STATUS_PENDING_APPROVAL,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        approved_by=None,
        approved_at=None,
        content_summary=content_summary,
        content_decisions=content_decisions,
        content_action_items=content_action_items,
    )
    db.add(minutes)
    db.flush()

    _populate_minutes_recipients(db, minutes)
    db.commit()
    db.refresh(minutes)
    return minutes


def get_meeting_minutes(
    db: Session,
    meeting_id: str,
) -> MeetingMinutes | None:
    """Retrieve existing meeting minutes with recipients if already generated."""
    return (
        db.query(MeetingMinutes)
        .filter(MeetingMinutes.meeting_id == meeting_id)
        .order_by(MeetingMinutes.created_at.desc(), MeetingMinutes.id.desc())
        .first()
    )


def approve_meeting_minutes(
    db: Session,
    meeting_id: str,
    approver_id: str,
) -> MeetingMinutes:
    """Approve meeting minutes for delivery.

    Enforces that minutes must exist.
    Transitions status to approved with organizer identifier and timestamp.
    Does not dispatch emails in this phase.
    """
    minutes = get_meeting_minutes(db, meeting_id)
    if minutes is None:
        raise ValueError("Meeting minutes not found.")

    if minutes.status != MINUTES_STATUS_APPROVED:
        minutes.status = MINUTES_STATUS_APPROVED
        minutes.approved_by = approver_id
        minutes.approved_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(minutes)

    return minutes


def render_minutes_email(
    meeting_title: str,
    minutes: MeetingMinutes,
    recipient_name: str,
) -> tuple[str, str]:
    """Compose the subject and plain-text body for a meeting minutes email."""
    subject = f"Meeting minutes: {meeting_title}"

    summary_section = (
        f"Summary\n-------\n{minutes.content_summary}\n\n"
        if minutes.content_summary
        else ""
    )

    decisions = minutes.content_decisions or []
    if decisions:
        decisions_lines = "\n".join(f"  - {d}" for d in decisions)
        decisions_section = f"Decisions\n---------\n{decisions_lines}\n\n"
    else:
        decisions_section = ""

    action_items = minutes.content_action_items or []
    if action_items:
        ai_lines = []
        for ai in action_items:
            owner = ai.get("owner_name") or "Unassigned"
            deadline = ai.get("deadline_text") or ai.get("deadline_at") or "TBD"
            priority = ai.get("priority", "")
            ai_lines.append(
                f"  - {ai.get('task_description', '')} "
                f"[{owner} / due {deadline} / {priority}]"
            )
        ai_section = "Action Items\n------------\n" + "\n".join(ai_lines) + "\n\n"
    else:
        ai_section = ""

    body = (
        f"Hello {recipient_name},\n\n"
        f"Please find the minutes for the meeting: {meeting_title}\n\n"
        f"{summary_section}"
        f"{decisions_section}"
        f"{ai_section}"
        "This message was sent automatically by the Office Operations system.\n"
    )
    return subject, body


def send_meeting_minutes_to_recipients(
    db: Session,
    meeting_id: str,
    *,
    retry_failed: bool = False,
) -> list[MeetingMinutesRecipient]:
    """Deliver approved meeting minutes to all pending (or failed) attendee recipients.

    Safety properties:
    - Enforces organizer approval: raises ValueError if minutes are not approved.
    - Per-recipient delivery: one recipient's failure does not affect others.
    - Idempotency: already-sent recipients are always skipped.
    - Retry support: re-attempts recipients in ``failed`` status when
      ``retry_failed=True``; never re-sends to ``sent`` recipients.
    - Crash safety: delivery_status is written to the DB *after* the SMTP call
      completes. If the process crashes between send and DB write the recipient
      remains in pending/failed and will be retried on the next call.
    """
    from app.task_service import send_email  # local import to avoid circular deps

    minutes = get_meeting_minutes(db, meeting_id)
    if minutes is None:
        raise ValueError("Meeting minutes not found.")
    if minutes.status != MINUTES_STATUS_APPROVED:
        raise ValueError(
            "Minutes must be approved before they can be sent. "
            f"Current status: {minutes.status}"
        )

    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError("Meeting not found.")

    results: list[MeetingMinutesRecipient] = []

    for recipient in list(minutes.recipients):
        if recipient.delivery_status == MINUTES_DELIVERY_STATUS_SENT:
            # Already delivered — never re-send.
            results.append(recipient)
            continue
        if (
            recipient.delivery_status == MINUTES_DELIVERY_STATUS_FAILED
            and not retry_failed
        ):
            # Previous failure; skip unless caller requested retry.
            results.append(recipient)
            continue

        subject, body = render_minutes_email(
            meeting.title, minutes, recipient.recipient_name
        )

        try:
            send_email(recipient.recipient_email, subject, body)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            error_msg = str(exc)
            recipient.delivery_status = MINUTES_DELIVERY_STATUS_FAILED
            recipient.error_message = error_msg
            recipient.sent_at = None
            logger.exception(
                "Minutes delivery failed minutes_id=%s recipient=%s",
                minutes.id,
                recipient.recipient_email,
            )
        else:
            recipient.delivery_status = MINUTES_DELIVERY_STATUS_SENT
            recipient.error_message = None
            recipient.sent_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(recipient)
        results.append(recipient)

    return results


def get_minutes_recipients(
    db: Session,
    meeting_id: str,
) -> list[MeetingMinutesRecipient]:
    """Retrieve the per-recipient delivery records for a meeting's minutes."""
    minutes = get_meeting_minutes(db, meeting_id)
    if minutes is None:
        return []
    return list(minutes.recipients)
