"""Cancellation, participant-change, notification, and audit operations (Story 5.4)."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import smtplib
import uuid

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.meeting.calendar_provider import CalendarProviderError, get_calendar_provider
from app.meeting.models import (
    CANCELLATION_STATUS_APPROVED,
    CANCELLATION_STATUS_PENDING,
    CANCELLATION_STATUS_REJECTED,
    Employee,
    Meeting,
    MeetingAuditLog,
    MeetingCancellationRequest,
    MeetingParticipant,
)
from app.meeting.schemas import (
    MeetingParticipantChangeRequest,
    MeetingParticipantBulkChangeRequest,
)
from app.meeting.service import is_authorized_rescheduler
from app.task_service import deliver_email

logger = logging.getLogger(__name__)
DEFAULT_CANCELLATION_APPROVAL_THRESHOLD = 20


def cancellation_approval_threshold() -> int:
    """Read the invitee threshold; organizer/delegate approval remains the approver policy."""
    try:
        return max(
            1,
            int(os.getenv(
                "MEETING_CANCELLATION_APPROVAL_ATTENDEE_THRESHOLD",
                str(DEFAULT_CANCELLATION_APPROVAL_THRESHOLD),
            )),
        )
    except ValueError:
        logger.warning("Invalid cancellation approval threshold; using %s.", DEFAULT_CANCELLATION_APPROVAL_THRESHOLD)
        return DEFAULT_CANCELLATION_APPROVAL_THRESHOLD


def _get_meeting(db: Session, meeting_id: str) -> Meeting:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    return meeting


def _require_authorized(db: Session, meeting: Meeting, actor_id: str) -> None:
    if not is_authorized_rescheduler(db, meeting, actor_id):
        raise HTTPException(
            status_code=403,
            detail="Only the meeting organizer or an authorized delegate can manage this meeting.",
        )


def _require_cancellable(meeting: Meeting) -> None:
    if meeting.status in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail=f"Meeting is already {meeting.status}.")
    if meeting.status in ("active", "instant", "in_progress"):
        raise HTTPException(status_code=409, detail="An in-progress meeting cannot be cancelled.")


def _attendee_rows(db: Session, meeting_id: str) -> list[MeetingParticipant]:
    return (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.meeting_id == meeting_id)
        .order_by(MeetingParticipant.id)
        .all()
    )


def attendee_count(db: Session, meeting: Meeting) -> int:
    """Count unique directory identities plus the organizer.

    Unresolved attendees are counted by exact e-mail, or by participant row if
    they have no e-mail. A directory mismatch fails closed rather than merging
    two potentially different people or allowing approval to be bypassed.
    """
    rows = _attendee_rows(db, meeting.id)
    user_ids = {row.user_id for row in rows if row.user_id}
    employees = (
        db.query(Employee).filter(Employee.id.in_(user_ids)).all()
        if user_ids
        else []
    )
    employees_by_id = {employee.id: employee for employee in employees}
    employees_by_email = {
        employee.email.strip().casefold(): employee
        for employee in db.query(Employee).all()
        if employee.email and employee.email.strip()
    }
    organizer = db.query(Employee).filter(Employee.id == meeting.organizer_id).first()
    organizer_identity = (
        f"employee:{organizer.id}" if organizer else f"user:{meeting.organizer_id}"
    )
    identities = {organizer_identity}
    for row in rows:
        email = row.email.strip().casefold() if row.email and row.email.strip() else None
        email_employee = employees_by_email.get(email) if email else None
        employee = employees_by_id.get(row.user_id) if row.user_id else None

        if employee:
            if email_employee and email_employee.id != employee.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Participant identity data is inconsistent for meeting '{meeting.id}'. "
                        "Resolve the user ID and directory e-mail mismatch before cancellation."
                    ),
                )
            identities.add(f"employee:{employee.id}")
        elif row.user_id == meeting.organizer_id:
            if email_employee and (organizer is None or email_employee.id != organizer.id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Organizer identity data is inconsistent for meeting '{meeting.id}'. "
                        "Resolve the directory e-mail mismatch before cancellation."
                    ),
                )
            identities.add(organizer_identity)
        elif row.user_id and email_employee:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Participant identity data is inconsistent for meeting '{meeting.id}'. "
                    "The user ID does not match the directory e-mail."
                ),
            )
        elif row.user_id:
            identities.add(f"unresolved-user:{row.user_id}")
        elif email_employee:
            identities.add(f"employee:{email_employee.id}")
        elif email:
            identities.add(f"email:{email}")
        else:
            identities.add(f"participant:{row.id}")
    return len(identities)


def _notification_recipients(
    db: Session,
    meeting: Meeting,
    rows: list[MeetingParticipant] | None = None,
) -> list[tuple[str | None, str]]:
    participants = rows if rows is not None else _attendee_rows(db, meeting.id)
    user_ids = {row.user_id for row in participants if row.user_id}
    employees = (
        db.query(Employee).filter(Employee.id.in_(user_ids)).all()
        if user_ids
        else []
    )
    employees_by_id = {employee.id: employee for employee in employees}
    recipients: dict[str, str] = {}
    missing: list[tuple[None, str]] = []
    for participant in participants:
        employee = employees_by_id.get(participant.user_id) if participant.user_id else None
        email = (participant.email or (employee.email if employee else None) or "").strip()
        if not email:
            missing.append((None, f"user:{participant.user_id}" if participant.user_id else f"participant:{participant.id}"))
            continue
        normalized = email.lower()
        recipients.setdefault(normalized, email)
    return [(email, email) for email in recipients] + missing


def _audit(
    db: Session,
    meeting_id: str,
    actor_id: str,
    action: str,
    details: dict,
) -> MeetingAuditLog:
    entry = MeetingAuditLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        actor_id=actor_id,
        action=action,
        details=details,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    return entry


def _send_notices(
    db: Session,
    meeting: Meeting,
    audit_entry: MeetingAuditLog,
    notice_kind: str,
    recipients: list[tuple[str | None, str]],
) -> tuple[list[str], list[str]]:
    sent: list[str] = []
    failures: list[str] = []
    outcomes: list[dict[str, str]] = []
    for email, recipient in recipients:
        if email is None:
            failures.append(recipient)
            outcomes.append({"recipient": recipient, "status": "failed", "error": "No email address is available."})
            continue
        if notice_kind == "cancelled":
            subject = f"Cancelled: {meeting.title}"
            body = (
                f"This meeting has been cancelled.\n\nTitle: {meeting.title}\n"
                f"Start: {meeting.start_at}\nEnd: {meeting.end_at}"
            )
        elif notice_kind == "removed":
            subject = f"Meeting update: {meeting.title}"
            body = f"You have been removed from this meeting.\n\nTitle: {meeting.title}\nStart: {meeting.start_at}\nEnd: {meeting.end_at}"
        else:
            subject = f"Invitation: {meeting.title}"
            body = (
                f"You have been invited to a meeting.\n\nTitle: {meeting.title}\n"
                f"When: {meeting.start_at} - {meeting.end_at}\nJoin URL: {meeting.join_url}"
            )
        try:
            deliver_email(email, subject, body)
        except Exception as exc:
            logger.exception("Meeting notice delivery failed meeting_id=%s recipient=%s", meeting.id, email)
            failures.append(email)
            outcomes.append({"recipient": email, "status": "failed", "error": str(exc)})
        else:
            sent.append(email)
            outcomes.append({"recipient": email, "status": "sent", "error": ""})
    audit_entry.details = {**audit_entry.details, "notifications": outcomes}
    db.commit()
    return sent, failures


def request_meeting_cancellation(
    db: Session,
    meeting_id: str,
    actor_id: str,
    reason: str | None = None,
) -> tuple[Meeting, MeetingCancellationRequest | None, list[str], list[str]]:
    meeting = _get_meeting(db, meeting_id)
    _require_authorized(db, meeting, actor_id)
    _require_cancellable(meeting)
    count = attendee_count(db, meeting)
    if count > cancellation_approval_threshold():
        existing = (
            db.query(MeetingCancellationRequest)
            .filter(
                MeetingCancellationRequest.meeting_id == meeting.id,
                MeetingCancellationRequest.status == CANCELLATION_STATUS_PENDING,
            )
            .first()
        )
        if existing:
            return meeting, existing, [], []
        request = MeetingCancellationRequest(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            requested_by=actor_id,
            reason=reason,
            status=CANCELLATION_STATUS_PENDING,
        )
        db.add(request)
        _audit(db, meeting.id, actor_id, "cancellation_requested", {
            "request_id": request.id,
            "status": CANCELLATION_STATUS_PENDING,
            "attendee_count": count,
            "approval_threshold": cancellation_approval_threshold(),
            "approver_policy": "meeting_organizer_or_authorized_delegate",
            "reason": reason,
        })
        db.commit()
        db.refresh(request)
        return meeting, request, [], []

    recipients = _notification_recipients(db, meeting)
    provider = get_calendar_provider()
    try:
        provider.cancel_event(db, meeting.id)
    except CalendarProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit_entry = _audit(db, meeting.id, actor_id, "meeting_cancelled", {
        "attendee_count": count,
        "reason": reason,
        "notifications": [],
    })
    db.commit()
    db.refresh(meeting)
    sent, failures = _send_notices(db, meeting, audit_entry, "cancelled", recipients)
    return meeting, None, sent, failures


def decide_meeting_cancellation(
    db: Session,
    request_id: str,
    decision: str,
    actor_id: str,
    note: str | None = None,
) -> tuple[MeetingCancellationRequest, Meeting | None, list[str], list[str]]:
    request = db.query(MeetingCancellationRequest).filter(MeetingCancellationRequest.id == request_id).first()
    if request is None:
        raise HTTPException(status_code=404, detail="Cancellation request not found.")
    meeting = _get_meeting(db, request.meeting_id)
    _require_authorized(db, meeting, actor_id)
    if request.status != CANCELLATION_STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"Cancellation request is already {request.status}.")
    if decision == "approve" and actor_id == request.requested_by:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The cancellation requester cannot approve their own high-attendance cancellation.",
        )

    now = datetime.now(timezone.utc)
    request.decided_by = actor_id
    request.decided_at = now
    request.decision_note = note
    if decision == "reject":
        request.status = CANCELLATION_STATUS_REJECTED
        _audit(db, meeting.id, actor_id, "cancellation_rejected", {
            "request_id": request.id,
            "requested_by": request.requested_by,
            "decision_note": note,
        })
        db.commit()
        db.refresh(request)
        return request, None, [], []

    _require_cancellable(meeting)
    recipients = _notification_recipients(db, meeting)
    try:
        get_calendar_provider().cancel_event(db, meeting.id)
    except CalendarProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.status = CANCELLATION_STATUS_APPROVED
    audit_entry = _audit(db, meeting.id, actor_id, "cancellation_approved", {
        "request_id": request.id,
        "requested_by": request.requested_by,
        "notifications": [],
        "decision_note": note,
    })
    db.commit()
    db.refresh(meeting)
    db.refresh(request)
    sent, failures = _send_notices(db, meeting, audit_entry, "cancelled", recipients)
    return request, meeting, sent, failures


def add_meeting_participant(
    db: Session,
    meeting_id: str,
    actor_id: str,
    payload: MeetingParticipantChangeRequest,
) -> tuple[Meeting, list[str], list[str]]:
    meeting = _get_meeting(db, meeting_id)
    _require_authorized(db, meeting, actor_id)
    if meeting.status in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail=f"Cannot change participants on a {meeting.status} meeting.")

    employee = None
    if payload.user_id:
        employee = db.query(Employee).filter(Employee.id == payload.user_id).first()
    email = payload.email.strip().lower() if payload.email else (employee.email.lower() if employee else None)
    if not email:
        raise HTTPException(status_code=422, detail="The participant must have a deliverable email address.")
    if employee and payload.email and employee.email.lower() != email:
        raise HTTPException(status_code=422, detail="The supplied email does not match the selected employee.")

    for participant in _attendee_rows(db, meeting.id):
        participant_email = participant.email.lower() if participant.email else None
        if participant_email == email or (payload.user_id and participant.user_id == payload.user_id):
            raise HTTPException(status_code=409, detail="This participant is already invited.")

    provider = get_calendar_provider()
    try:
        provider.add_attendee(db, meeting.id, payload.user_id or (employee.id if employee else None), email)
    except CalendarProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit_entry = _audit(db, meeting.id, actor_id, "participant_added", {
        "user_id": payload.user_id or (employee.id if employee else None),
        "email": email,
        "notifications": [],
    })
    db.commit()
    db.refresh(meeting)
    sent, failures = _send_notices(db, meeting, audit_entry, "added", [(email, email)])
    return meeting, sent, failures


def add_meeting_participants(
    db: Session,
    meeting_id: str,
    actor_id: str,
    payload: MeetingParticipantBulkChangeRequest,
) -> tuple[Meeting, dict[str, list[str]]]:
    meeting = _get_meeting(db, meeting_id)
    _require_authorized(db, meeting, actor_id)
    if meeting.status in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail=f"Cannot change participants on a {meeting.status} meeting.")

    outcomes = {
        "added": [],
        "already_present": [],
        "duplicate_addresses": [],
        "invalid_addresses": [],
        "failed_addresses": [],
        "notifications_sent": [],
        "notification_failures": [],
    }
    existing_emails = {
        row.email.strip().casefold()
        for row in _attendee_rows(db, meeting.id)
        if row.email and row.email.strip()
    }
    seen: set[str] = set()

    for raw_email in payload.emails:
        try:
            participant_request = MeetingParticipantChangeRequest(email=raw_email)
        except ValidationError:
            outcomes["invalid_addresses"].append(raw_email.strip() or "(empty address)")
            continue

        email = participant_request.email
        if email is None:
            outcomes["invalid_addresses"].append(raw_email.strip() or "(empty address)")
            continue
        if email in seen:
            outcomes["duplicate_addresses"].append(email)
            continue
        seen.add(email)
        if email in existing_emails:
            outcomes["already_present"].append(email)
            continue

        try:
            _, sent, failures = add_meeting_participant(
                db, meeting.id, actor_id, participant_request
            )
        except HTTPException as exc:
            if exc.status_code == 409 and exc.detail == "This participant is already invited.":
                outcomes["already_present"].append(email)
                existing_emails.add(email)
                continue
            if exc.status_code == 502:
                outcomes["failed_addresses"].append(email)
                continue
            raise

        outcomes["added"].append(email)
        outcomes["notifications_sent"].extend(sent)
        outcomes["notification_failures"].extend(failures)
        existing_emails.add(email)

    return meeting, outcomes


def remove_meeting_participant(
    db: Session,
    meeting_id: str,
    participant_id: str,
    actor_id: str,
) -> tuple[Meeting, list[str], list[str]]:
    meeting = _get_meeting(db, meeting_id)
    _require_authorized(db, meeting, actor_id)
    if meeting.status in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail=f"Cannot change participants on a {meeting.status} meeting.")
    participant = (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.id == participant_id, MeetingParticipant.meeting_id == meeting.id)
        .first()
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="Meeting participant not found.")
    recipients = _notification_recipients(db, meeting, [participant])
    details = {
        "participant_id": participant.id,
        "user_id": participant.user_id,
        "email": participant.email,
        "notifications": [],
    }
    try:
        get_calendar_provider().remove_attendee(db, meeting.id, participant.id)
    except CalendarProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit_entry = _audit(db, meeting.id, actor_id, "participant_removed", details)
    db.commit()
    db.refresh(meeting)
    sent, failures = _send_notices(db, meeting, audit_entry, "removed", recipients)
    return meeting, sent, failures


def list_pending_cancellations(db: Session, actor_id: str) -> list[tuple[MeetingCancellationRequest, Meeting, int]]:
    requests = (
        db.query(MeetingCancellationRequest)
        .filter(MeetingCancellationRequest.status == CANCELLATION_STATUS_PENDING)
        .order_by(MeetingCancellationRequest.created_at.asc())
        .all()
    )
    results = []
    for request in requests:
        meeting = db.query(Meeting).filter(Meeting.id == request.meeting_id).first()
        if meeting and is_authorized_rescheduler(db, meeting, actor_id):
            results.append((request, meeting, attendee_count(db, meeting)))
    return results


def list_meeting_audit(db: Session, meeting_id: str, actor_id: str) -> list[MeetingAuditLog]:
    meeting = _get_meeting(db, meeting_id)
    _require_authorized(db, meeting, actor_id)
    return (
        db.query(MeetingAuditLog)
        .filter(MeetingAuditLog.meeting_id == meeting_id)
        .order_by(MeetingAuditLog.created_at.desc(), MeetingAuditLog.id.desc())
        .all()
    )


def get_meeting_participants(
    db: Session,
    meeting_id: str,
    actor_id: str,
) -> list[tuple[MeetingParticipant, Employee | None]]:
    meeting = _get_meeting(db, meeting_id)
    _require_authorized(db, meeting, actor_id)
    participants = _attendee_rows(db, meeting_id)
    user_ids = {participant.user_id for participant in participants if participant.user_id}
    employees = (
        db.query(Employee).filter(Employee.id.in_(user_ids)).all()
        if user_ids
        else []
    )
    employees_by_id = {employee.id: employee for employee in employees}
    return [(participant, employees_by_id.get(participant.user_id)) for participant in participants]