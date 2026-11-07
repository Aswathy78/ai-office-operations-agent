"""Natural-language meeting scheduling service (Story 5.1 / FR-23).

Parses natural-language scheduling requests (e.g. "Schedule a project meeting
tomorrow at 10 AM with the development team"), resolves attendees from the
employee directory and directory groups, checks attendee availability/free-busy
information, enforces conflict prevention, creates the event via the configured
CalendarProvider, and dispatches attendee invitations.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import logging
import os
import re
import smtplib
from typing import Literal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.email_draft_service import (
    _active_employees,
    _group_members,
    detect_ambiguous_employee_names,
    detect_groups,
    detect_named_employees,
)
from app.meeting.calendar_provider import (
    CalendarConflictError,
    CalendarEvent,
    FreeBusyConflict,
    get_calendar_provider,
    _to_naive_utc,
)
from app.meeting.employee_matching import match_employee_name
from app.meeting.models import (
    Employee,
    Meeting,
    MeetingDelegate,
    MeetingParticipant,
    MeetingRescheduleRequest,
    RESCHEDULE_STATUS_APPROVED,
    RESCHEDULE_STATUS_PENDING,
    RESCHEDULE_STATUS_REJECTED,
)
from app.meeting.service import is_authorized_rescheduler
from app.task_service import deliver_email

logger = logging.getLogger(__name__)

DEFAULT_DURATION_MINUTES = 30
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ScheduledMeetingResult:
    status: Literal["Completed", "needs_clarification", "conflict", "error", "pending_approval"]
    meeting: CalendarEvent | None = None
    clarifying_question: str | None = None
    conflicts: list[FreeBusyConflict] = field(default_factory=list)
    alternative_slots: list[tuple[datetime, datetime]] = field(default_factory=list)
    message: str | None = None
    resolved_attendees: list[dict] = field(default_factory=list)
    invitations_sent: list[str] = field(default_factory=list)
    invitation_failures: list[str] = field(default_factory=list)
    reschedule_request_id: str | None = None


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_organization_timezone() -> ZoneInfo | timezone:
    """Retrieve the configured organisation timezone, defaulting to UTC."""
    tz_name = os.getenv("ORGANIZATION_TIMEZONE", "UTC").strip()
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, Exception):
        logger.warning("Invalid ORGANIZATION_TIMEZONE '%s'; falling back to UTC.", tz_name)
        try:
            return ZoneInfo("UTC")
        except Exception:
            return timezone.utc


def parse_duration_minutes(text: str) -> int:
    """Extract meeting duration if specified, otherwise return default of 30 minutes."""
    match = re.search(r"\b(?:for\s+)?(\d+)\s*(hour|hr|minute|min)s?\b", text, re.IGNORECASE)
    if not match:
        return DEFAULT_DURATION_MINUTES
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit in ("hour", "hr"):
        return amount * 60
    return amount


def parse_time_of_day(text: str) -> tuple[int, int] | None:
    """Parse a time string such as '10 AM', '10:30am', '2 PM', or '14:00'."""
    # 12-hour format: '10 AM', '10:30 pm', '2.30 PM', '2 30 PM', 'at 9am'
    match12 = re.search(
        r"\b(?:at\s+)?(1[0-2]|0?[1-9])(?:(?::|\.|\s+)([0-5]\d))?\s*(am|pm)\b",
        text,
        re.IGNORECASE,
    )
    if match12:
        hour = int(match12.group(1))
        minute = int(match12.group(2) or 0)
        meridiem = match12.group(3).lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return hour, minute

    # 24-hour format: 'at 14:00', '15:30'
    match24 = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", text, re.IGNORECASE)
    if match24:
        return int(match24.group(1)), int(match24.group(2))

    return None


def parse_meeting_date(text: str, reference_local: datetime) -> date | None:
    """Parse relative or explicit date strings relative to reference local time."""
    lower = text.lower()

    if "tomorrow" in lower or "the next day" in lower:
        return (reference_local + timedelta(days=1)).date()

    if "today" in lower:
        return reference_local.date()

    in_days_match = re.search(r"\bin\s+(\d+)\s+days?\b", lower)
    if in_days_match:
        return (reference_local + timedelta(days=int(in_days_match.group(1)))).date()

    month_names = {
        name.lower(): month
        for month in range(1, 13)
        for name in (calendar.month_name[month], calendar.month_abbr[month])
    }
    month_pattern = "|".join(
        re.escape(name)
        for name in sorted(month_names, key=len, reverse=True)
    )
    month_date_match = re.search(
        rf"\b(?:(?P<day_first>\d{{1,2}})(?:st|nd|rd|th)?\s+"
        rf"(?P<month_first>{month_pattern})\.?|"
        rf"(?P<month_last>{month_pattern})\.?\s+"
        rf"(?P<day_last>\d{{1,2}})(?:st|nd|rd|th)?)"
        rf"(?:,?\s+(?P<year>\d{{4}}))?\b",
        lower,
    )
    if month_date_match:
        day = int(month_date_match.group("day_first") or month_date_match.group("day_last"))
        month_name = month_date_match.group("month_first") or month_date_match.group("month_last")
        month = month_names[month_name.rstrip(".")]
        year = int(month_date_match.group("year") or reference_local.year)
        try:
            explicit_date = date(year, month, day)
        except ValueError:
            return None
        if month_date_match.group("year") is None and explicit_date < reference_local.date():
            try:
                explicit_date = explicit_date.replace(year=year + 1)
            except ValueError:
                return None
        return explicit_date

    # Weekday detection: 'on Monday', 'next Friday', 'this Wednesday'
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    weekday_match = re.search(
        r"\b(?:(next|this|on)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lower,
    )
    if weekday_match:
        modifier = weekday_match.group(1) or "this"
        target_weekday = weekdays[weekday_match.group(2)]
        current_weekday = reference_local.weekday()
        days_ahead = (target_weekday - current_weekday) % 7
        if modifier == "next":
            days_ahead += 7
        elif days_ahead == 0:
            days_ahead = 7
        return (reference_local + timedelta(days=days_ahead)).date()

    # Explicit date: e.g. 'Oct 25', 'October 25', '2026-10-25'
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass

    return None


def extract_meeting_title(text: str) -> str:
    """Extract a concise meeting topic/title from the request, e.g. 'project meeting'."""
    patterns = [
        r"\bschedule\s+(?:a\s+|an\s+)?(.+?)\s+(?:tomorrow|today|next|this|on|at|for|with)\b",
        r"\bbook\s+(?:a\s+|an\s+)?(.+?)\s+(?:tomorrow|today|next|this|on|at|for|with)\b",
        r"\bset\s+up\s+(?:a\s+|an\s+)?(.+?)\s+(?:tomorrow|today|next|this|on|at|for|with)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            # If candidate is just "meeting", keep looking or capitalize nicely
            if candidate and len(candidate) > 1:
                return candidate.title()

    # Fallback to general meeting title
    return "Project Meeting"


def resolve_attendees(
    db: Session,
    prompt: str,
) -> tuple[list[Employee], list[str], str | None]:
    """Resolve active employees and directory groups from the prompt.

    Returns:
        (resolved_employees, raw_emails, ambiguous_or_missing_note)
    """
    all_employees = _active_employees(db)
    resolved_by_id: dict[str, Employee] = {}

    # 1. Directory Groups (e.g. 'the development team', 'finance team', 'everyone')
    detected_group_names = detect_groups(db, prompt)
    for group_name in detected_group_names:
        for member in _group_members(all_employees, group_name):
            resolved_by_id[member.id] = member

    ambiguous_names = detect_ambiguous_employee_names(db, prompt)
    if ambiguous_names:
        name, candidates = next(iter(ambiguous_names.items()))
        options = ", ".join(candidates)
        return [], [], f"I found multiple employees named {name}: {options}. Which one did you mean?"

    # 2. Named Employees (full name or unique first name)
    for employee in detect_named_employees(db, prompt):
        resolved_by_id[employee.id] = employee

    # 3. Direct email addresses
    raw_emails = EMAIL_PATTERN.findall(prompt)
    for email in raw_emails:
        emp = db.query(Employee).filter(Employee.email.ilike(email.strip())).first()
        if emp:
            resolved_by_id[emp.id] = emp

    # 4. Check for 'with ...' clauses for ambiguous or unresolvable attendee names
    with_match = re.search(r"\bwith\s+([^,.\n;]+)", prompt, re.IGNORECASE)
    if with_match:
        with_clause = with_match.group(1).strip()
        # Clean trailing time/date phrases if caught in the clause
        with_clause = re.split(r"\b(tomorrow|today|at\s+\d+|on\s+\w+|for\s+\d+)\b", with_clause, flags=re.I)[0].strip()

        # If we didn't resolve any attendee from groups or named employees, check with_clause
        if not resolved_by_id and not raw_emails and with_clause:
            # Check if this with_clause is ambiguous or unmatched
            match_res = match_employee_name(db, with_clause)
            if match_res.status == "ambiguous_match" and match_res.candidate_names:
                options = " or ".join(match_res.candidate_names[:2])
                return (
                    [],
                    [],
                    f"Did you mean {options}?",
                )
            if match_res.status in ("no_match", "missing_owner"):
                return (
                    [],
                    [],
                    f"I couldn't find an employee or team matching '{with_clause}'. Who should be invited?",
                )

    return list(resolved_by_id.values()), raw_emails, None


def send_meeting_invitations(
    meeting: CalendarEvent,
    organizer_name: str,
    attendees: list[Employee],
    timezone_name: str,
    raw_emails: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Send email invitations to resolved meeting attendees."""
    sent: list[str] = []
    failures: list[str] = []
    subject = f"Invitation: {meeting.title}"

    # Convert naive UTC meeting start/end into the target organization timezone
    if meeting.start_at.tzinfo is None:
        start_utc = meeting.start_at.replace(tzinfo=timezone.utc)
        end_utc = meeting.end_at.replace(tzinfo=timezone.utc)
    else:
        start_utc = meeting.start_at.astimezone(timezone.utc)
        end_utc = meeting.end_at.astimezone(timezone.utc)

    try:
        org_tz = ZoneInfo(timezone_name)
    except Exception:
        org_tz = timezone.utc

    start_local = start_utc.astimezone(org_tz)
    end_local = end_utc.astimezone(org_tz)

    start_str = start_local.strftime("%A, %B %d, %Y at %I:%M %p")
    end_str = end_local.strftime("%I:%M %p")

    # Collect recipient list (resolved employees and any direct raw emails)
    recipient_map: dict[str, str] = {
        att.email.strip().casefold(): f"{att.name} <{att.email.strip()}>"
        for att in attendees
        if att.email and att.email.strip()
    }
    if raw_emails:
        for email in raw_emails:
            normalized_email = email.strip().casefold()
            if normalized_email and normalized_email not in recipient_map:
                recipient_map[normalized_email] = email.strip()

    body_lines = [
        "You have been invited to a meeting.",
        "",
        f"Title: {meeting.title}",
        f"When: {start_str} - {end_str} ({timezone_name})",
        f"Organizer: {organizer_name}",
        f"Join URL: {meeting.join_url or 'N/A'}",
        "",
        "Attendees:",
    ]
    for display in recipient_map.values():
        body_lines.append(f"- {display}")
    body = "\n".join(body_lines)

    for email in recipient_map.keys():
        try:
            deliver_email(email, subject, body)
            sent.append(email)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            logger.warning(
                "Meeting invitation delivery failed recipient=%s error_type=%s",
                email,
                type(exc).__name__,
            )
            failures.append(email)

    return sent, failures


def _employee_timezone(employee: Employee, organization_timezone: ZoneInfo | timezone):
    if not employee.timezone_name:
        return organization_timezone
    try:
        return ZoneInfo(employee.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Invalid employee timezone '%s' for employee %s; using organization timezone.",
            employee.timezone_name,
            employee.id,
        )
        return organization_timezone


def _fits_attendee_working_hours(
    start_at: datetime,
    end_at: datetime,
    attendee_emails: list[str],
    employees_by_email: dict[str, Employee],
    organization_timezone: ZoneInfo | timezone,
    default_for_unknown_attendees: bool = False,
) -> bool:
    for email in attendee_emails:
        employee = employees_by_email.get(email.lower())
        if employee is None:
            if not default_for_unknown_attendees:
                continue
            attendee_timezone = organization_timezone
            working_days = [0, 1, 2, 3, 4]
            start_text = "09:00"
            end_text = "17:00"
        else:
            attendee_timezone = _employee_timezone(employee, organization_timezone)
            working_days = employee.working_days
            start_text = employee.working_hours_start
            end_text = employee.working_hours_end
        local_start = start_at.astimezone(attendee_timezone)
        local_end = end_at.astimezone(attendee_timezone)
        if local_start.date() != local_end.date():
            return False

        try:
            work_start = time.fromisoformat(start_text)
            work_end = time.fromisoformat(end_text)
        except ValueError:
            logger.warning("Invalid working hours for attendee %s; using 09:00-17:00.", email)
            work_start, work_end = time(9), time(17)
        if (
            local_start.weekday() not in working_days
            or local_start.time().replace(tzinfo=None) < work_start
            or local_end.time().replace(tzinfo=None) > work_end
        ):
            return False
    return True


def find_alternative_slots(
    db: Session,
    requested_start_at: datetime,
    duration_minutes: int,
    attendee_emails: list[str],
    provider=None,
    limit: int = 5,
    exclude_meeting_id: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Find the nearest future slots that fit all invitees and are conflict-free."""
    if requested_start_at.tzinfo is None:
        requested_start_at = requested_start_at.replace(tzinfo=timezone.utc)
    else:
        requested_start_at = requested_start_at.astimezone(timezone.utc)
    provider = provider or get_calendar_provider()
    organization_timezone = get_organization_timezone()
    normalized_emails = list(dict.fromkeys(email.strip().lower() for email in attendee_emails if email.strip()))
    employees_by_email = {
        employee.email.lower(): employee
        for employee in db.query(Employee).all()
        if employee.email and employee.email.lower() in normalized_emails
    }
    duration = timedelta(minutes=duration_minutes)
    slots: list[tuple[datetime, datetime]] = []

    # Search forward on a 30-minute grid, bounded to two weeks.
    for step in range(1, 14 * 48 + 1):
        slot_start = requested_start_at + timedelta(minutes=30 * step)
        slot_end = slot_start + duration
        if not _fits_attendee_working_hours(
            slot_start,
            slot_end,
            normalized_emails,
            employees_by_email,
            organization_timezone,
            default_for_unknown_attendees=True,
        ):
            continue
        availability = provider.check_availability(
            db, normalized_emails, slot_start, slot_end, exclude_meeting_id=exclude_meeting_id
        )
        if not availability.has_conflicts:
            slots.append((slot_start, slot_end))
            if len(slots) >= limit:
                break
    return slots


def schedule_meeting_from_natural_language(
    db: Session,
    prompt: str,
    organizer_id: str,
    reference_time: datetime | None = None,
    selected_start_at: datetime | None = None,
) -> ScheduledMeetingResult:
    """Core entrypoint for Story 5.1: Natural-Language Meeting Scheduling."""
    prompt = prompt.strip()
    if not prompt:
        return ScheduledMeetingResult(
            status="error",
            message="Please provide meeting scheduling details.",
        )

    # Story 5.3: Check if prompt expresses rescheduling intent
    if is_reschedule_intent(prompt):
        return reschedule_meeting_from_natural_language(
            db=db,
            prompt=prompt,
            requester_id=organizer_id,
            reference_time=reference_time,
            selected_start_at=selected_start_at,
        )

    # 1. Resolve Timezone and Local Reference Time
    org_tz = get_organization_timezone()
    ref_utc = reference_time or datetime.now(timezone.utc)
    ref_local = ref_utc.astimezone(org_tz)

    # 2. Parse Date, Time, Duration, and Title
    if selected_start_at is None:
        target_date = parse_meeting_date(prompt, ref_local)
        time_of_day = parse_time_of_day(prompt)

        if target_date is None:
            if time_of_day is None:
                return ScheduledMeetingResult(
                    status="needs_clarification",
                    clarifying_question="When would you like to schedule the meeting (date and time)?",
                )
            target_date = (ref_local + timedelta(days=1)).date()

        if time_of_day is None:
            return ScheduledMeetingResult(
                status="needs_clarification",
                clarifying_question=f"What time on {target_date.strftime('%A, %B %d')} should the meeting start?",
            )

        hour, minute = time_of_day
        start_local = datetime.combine(target_date, time(hour, minute), tzinfo=org_tz)
        start_utc = start_local.astimezone(timezone.utc)
    else:
        start_utc = selected_start_at
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        else:
            start_utc = start_utc.astimezone(timezone.utc)
        start_local = start_utc.astimezone(org_tz)

    duration_minutes = parse_duration_minutes(prompt)
    end_utc = start_utc + timedelta(minutes=duration_minutes)

    title = extract_meeting_title(prompt)

    # 3. Resolve Attendees from directory & groups
    attendees, raw_emails, clarification = resolve_attendees(db, prompt)

    if clarification:
        return ScheduledMeetingResult(
            status="needs_clarification",
            clarifying_question=clarification,
        )

    all_attendee_emails = list(dict.fromkeys(
        [emp.email.lower() for emp in attendees] + [e.lower() for e in raw_emails]
    ))

    # Acceptance Criterion 9: If attendees are missing, ask one clarifying question
    if not all_attendee_emails:
        return ScheduledMeetingResult(
            status="needs_clarification",
            clarifying_question="Who should be invited to this meeting? You can invite individual employees or a team such as the development team.",
        )

    if selected_start_at is not None:
        employees_by_email = {
            employee.email.lower(): employee
            for employee in db.query(Employee).all()
            if employee.email and employee.email.lower() in all_attendee_emails
        }
        if not _fits_attendee_working_hours(
            start_utc,
            end_utc,
            all_attendee_emails,
            employees_by_email,
            org_tz,
            default_for_unknown_attendees=True,
        ):
            alternatives = find_alternative_slots(
                db,
                start_utc,
                duration_minutes,
                all_attendee_emails,
            )
            invalid_slot_conflicts = [
                FreeBusyConflict(
                    attendee_email=email,
                    attendee_name=(
                        employees_by_email[email].name if email in employees_by_email else None
                    ),
                    conflicting_event_title="Outside working hours",
                    conflicting_start_at=start_utc,
                    conflicting_end_at=end_utc,
                )
                for email in all_attendee_emails
            ]
            return ScheduledMeetingResult(
                status="conflict",
                conflicts=invalid_slot_conflicts,
                alternative_slots=alternatives,
                message="The selected slot is outside one or more attendees' working hours. Please choose another slot.",
            )

    # 4. Check Availability & Free-Busy (Acceptance Criteria 4 & 5)
    try:
        provider = get_calendar_provider()
        availability = provider.check_availability(
            db,
            all_attendee_emails,
            start_utc,
            end_utc,
        )
    except Exception:
        logger.exception("Failed to check availability with the calendar provider.")
        return ScheduledMeetingResult(
            status="error",
            message=(
                "The configured calendar provider is unavailable; "
                "the meeting was not created."
            ),
        )

    if availability.has_conflicts:
        conflict_descriptions = [
            f"{c.attendee_name or c.attendee_email} is busy with '{c.conflicting_event_title}'"
            for c in availability.conflicts
        ]
        alternatives = find_alternative_slots(
            db,
            start_utc,
            duration_minutes,
            all_attendee_emails,
            provider=provider,
        )
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=availability.conflicts,
            alternative_slots=alternatives,
            message=(
                f"Cannot schedule meeting due to conflicts: {', '.join(conflict_descriptions)}. "
                "Please choose another time slot."
            ),
        )

    # 5. Create Meeting via Configured Calendar Provider & Verify (Acceptance Criteria 6 & 8)
    try:
        event = provider.create_event(
            db=db,
            title=title,
            start_at=start_utc,
            end_at=end_utc,
            organizer_id=organizer_id,
            attendee_emails=all_attendee_emails,
            agenda=f"Scheduled via AI Assistant: {prompt}",
        )
    except CalendarConflictError as exc:
        alternatives = find_alternative_slots(
            db,
            start_utc,
            duration_minutes,
            all_attendee_emails,
            provider=provider,
        )
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=exc.conflicts,
            alternative_slots=alternatives,
            message=str(exc),
        )
    except Exception as exc:
        logger.exception("Failed to create meeting in calendar provider.")
        return ScheduledMeetingResult(
            status="error",
            message=f"Failed to create meeting: {str(exc)}",
        )

    # 6. Send Invitations (Acceptance Criterion 7)
    organizer_emp = db.query(Employee).filter(Employee.id == organizer_id).first()
    organizer_name = organizer_emp.name if organizer_emp else "Meeting Organizer"
    invitations_sent, invitation_failures = send_meeting_invitations(
        meeting=event,
        organizer_name=organizer_name,
        attendees=attendees,
        timezone_name=str(org_tz),
        raw_emails=raw_emails,
    )

    attendee_details = [
        {"id": emp.id, "name": emp.name, "email": emp.email, "team": emp.team}
        for emp in attendees
    ]

    start_local_formatted = start_local.strftime("%A, %B %d at %I:%M %p")
    confirmation_message = (
        f"Scheduled '{event.title}' for {start_local_formatted} ({duration_minutes} minutes) "
        f"with {len(all_attendee_emails)} attendee(s). Join link: {event.join_url}"
    )

    if invitation_failures:
        failed_recipients = ", ".join(invitation_failures)
        return ScheduledMeetingResult(
            status="error",
            meeting=event,
            message=(
                f"Meeting '{event.title}' was created, but invitations could not be "
                f"delivered to: {failed_recipients}. Join link: {event.join_url}"
            ),
            resolved_attendees=attendee_details,
            invitations_sent=invitations_sent,
            invitation_failures=invitation_failures,
        )

    return ScheduledMeetingResult(
        status="Completed",
        meeting=event,
        message=confirmation_message,
        resolved_attendees=attendee_details,
        invitations_sent=invitations_sent,
        invitation_failures=[],
    )


# ---------------------------------------------------------------------------
# Story 5.3: Meeting Rescheduling by Chat
# ---------------------------------------------------------------------------


def is_reschedule_intent(prompt: str) -> bool:
    """Check if the prompt indicates a meeting reschedule request."""
    p = prompt.strip().lower()
    if not p:
        return False
    if re.match(r"^(?:email|send\s+(?:an?\s+)?email|draft\s+(?:an?\s+)?email)\b", p):
        return False
    if re.search(r"\b(?:move|shift|push|postpone|delay)\b", p):
        return True
    if re.search(r"\breschedule\b", p) and re.search(r"\b(?:to|for|at)\b", p):
        return True
    if re.search(r"\bchange\s+(?:the\s+)?(?:time|date|schedule)\b", p):
        return True
    return False


def _split_reschedule_prompt(prompt: str, ref_local: datetime) -> tuple[str, str | None]:
    """Split prompt into (source_meeting_ref, target_time_clause)."""
    matches = list(re.finditer(r"\b(?:to|for)\s+", prompt, re.IGNORECASE))
    for m in reversed(matches):
        candidate_target = prompt[m.end():].strip()
        if (
            parse_time_of_day(candidate_target) is not None
            or parse_meeting_date(candidate_target, ref_local) is not None
        ):
            source_part = prompt[: m.start()].strip()
            return source_part, candidate_target
    return prompt, None


def send_meeting_rescheduled_notifications(
    meeting: CalendarEvent,
    organizer_name: str,
    attendees: list[Employee],
    timezone_name: str,
    previous_start: datetime,
    previous_end: datetime,
    raw_emails: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Send email notifications to attendees when a meeting is rescheduled (Story 5.3)."""
    sent: list[str] = []
    failures: list[str] = []
    subject = f"Rescheduled: {meeting.title}"

    try:
        org_tz = ZoneInfo(timezone_name)
    except Exception:
        org_tz = timezone.utc

    def _format_window(start: datetime, end: datetime) -> str:
        s_utc = (
            start.replace(tzinfo=timezone.utc)
            if start.tzinfo is None
            else start.astimezone(timezone.utc)
        )
        e_utc = (
            end.replace(tzinfo=timezone.utc)
            if end.tzinfo is None
            else end.astimezone(timezone.utc)
        )
        s_loc = s_utc.astimezone(org_tz)
        e_loc = e_utc.astimezone(org_tz)
        return (
            f"{s_loc.strftime('%A, %B %d, %Y at %I:%M %p')} - "
            f"{e_loc.strftime('%I:%M %p')} ({timezone_name})"
        )

    prev_str = _format_window(previous_start, previous_end)
    new_str = _format_window(meeting.start_at, meeting.end_at)

    recipient_map: dict[str, str] = {
        att.email.lower(): f"{att.name} <{att.email}>" for att in attendees if att.email
    }
    if raw_emails:
        for email in raw_emails:
            e_lower = email.strip().lower()
            if e_lower and e_lower not in recipient_map:
                recipient_map[e_lower] = email.strip()

    body_lines = [
        "A meeting has been rescheduled.",
        "",
        f"Title: {meeting.title}",
        f"Previous Time: {prev_str}",
        f"New Time: {new_str}",
        f"Organizer: {organizer_name}",
        f"Join URL: {meeting.join_url or 'N/A'}",
        "",
        "Attendees:",
    ]
    for display in recipient_map.values():
        body_lines.append(f"- {display}")
    body = "\n".join(body_lines)

    for email in recipient_map.keys():
        try:
            deliver_email(email, subject, body)
            sent.append(email)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            logger.warning(
                "Meeting reschedule notification delivery failed recipient=%s error_type=%s",
                email,
                type(exc).__name__,
            )
            failures.append(email)

    return sent, failures


def find_target_meeting_to_reschedule(
    db: Session,
    source_ref: str,
    requester_id: str,
    org_tz: ZoneInfo | timezone,
    ref_local: datetime,
) -> tuple[Meeting | None, list[Meeting], str | None]:
    """Identify the existing meeting to reschedule from the prompt reference.

    Returns:
        (target_meeting, candidate_matches, clarification_or_error_message)
    """
    source_date = parse_meeting_date(source_ref, ref_local)
    source_time = parse_time_of_day(source_ref)

    # Extract keywords from source_ref, ignoring common stop words
    words = re.findall(r"[A-Za-z0-9]+", source_ref.lower())
    stopwords = {
        "move",
        "reschedule",
        "shift",
        "push",
        "postpone",
        "delay",
        "the",
        "a",
        "an",
        "tomorrow",
        "tomorrows",
        "today",
        "todays",
        "yesterday",
        "meeting",
        "meetings",
        "sync",
        "call",
        "session",
        "discussion",
        "at",
        "on",
        "with",
        "for",
        "from",
        "my",
        "our",
        "next",
        "this",
    }
    keywords = [w for w in words if w not in stopwords and len(w) > 1]

    # Check for attendee in source_ref e.g. "with Marcus"
    with_match = re.search(r"\bwith\s+([^,.\n;]+)", source_ref, re.IGNORECASE)
    with_clause = with_match.group(1).strip() if with_match else None

    # Query all active/scheduled meetings
    candidates = (
        db.query(Meeting)
        .filter(Meeting.status.in_(["scheduled", "instant", "in_progress"]))
        .all()
    )

    # 1. Date filter
    if source_date is not None:
        candidates = [
            m
            for m in candidates
            if _as_utc(m.start_at).astimezone(org_tz).date() == source_date
        ]

    # 2. Time filter
    if source_time is not None:
        candidates = [
            m
            for m in candidates
            if (
                _as_utc(m.start_at).astimezone(org_tz).hour == source_time[0]
                and _as_utc(m.start_at).astimezone(org_tz).minute == source_time[1]
            )
        ]

    # 3. Keywords in title
    if keywords:
        matching_title = [
            m for m in candidates if any(kw in m.title.lower() for kw in keywords)
        ]
        if matching_title:
            candidates = matching_title

    # 4. Attendee filtering if specified
    if with_clause:
        with_lower = with_clause.lower()
        matching_att = []
        for m in candidates:
            parts = (
                db.query(MeetingParticipant)
                .filter(MeetingParticipant.meeting_id == m.id)
                .all()
            )
            part_emails = [p.email.lower() for p in parts if p.email]
            part_users = [p.user_id for p in parts if p.user_id]
            part_names = [
                emp.name.lower()
                for emp in db.query(Employee).filter(Employee.id.in_(part_users)).all()
            ]
            if any(with_lower in email for email in part_emails) or any(
                with_lower in name for name in part_names
            ):
                matching_att.append(m)
        if matching_att:
            candidates = matching_att

    # 5. Prioritize meetings where caller is involved
    if len(candidates) > 1 and requester_id:
        involved = []
        for m in candidates:
            if m.organizer_id == requester_id:
                involved.append(m)
            elif (
                db.query(MeetingParticipant)
                .filter(
                    MeetingParticipant.meeting_id == m.id,
                    MeetingParticipant.user_id == requester_id,
                )
                .first()
                is not None
            ):
                involved.append(m)
            elif (
                db.query(MeetingDelegate)
                .filter(
                    MeetingDelegate.organizer_id == m.organizer_id,
                    MeetingDelegate.delegate_id == requester_id,
                    (MeetingDelegate.meeting_id == m.id)
                    | (MeetingDelegate.meeting_id.is_(None)),
                )
                .first()
                is not None
            ):
                involved.append(m)

        if len(involved) == 1:
            candidates = involved
        elif len(involved) > 1:
            candidates = involved

    if not candidates:
        date_str = source_date.strftime("%A, %B %d") if source_date else "the requested date"
        return None, [], f"I couldn't find a meeting on {date_str} to reschedule."

    if len(candidates) > 1:
        descriptions = [
            f"'{m.title}' ({_as_utc(m.start_at).astimezone(org_tz).strftime('%a, %b %d at %I:%M %p')})"
            for m in candidates
        ]
        clarifying_question = (
            f"You have multiple matching meetings: {', '.join(descriptions)}. "
            "Which meeting would you like to reschedule?"
        )
        return None, candidates, clarifying_question

    return candidates[0], candidates, None


def reschedule_meeting_from_natural_language(
    db: Session,
    prompt: str,
    requester_id: str,
    reference_time: datetime | None = None,
    selected_start_at: datetime | None = None,
) -> ScheduledMeetingResult:
    """Core entrypoint for Story 5.3: Meeting Rescheduling by Chat."""
    prompt = prompt.strip()
    if not prompt:
        return ScheduledMeetingResult(
            status="error",
            message="Please provide meeting rescheduling details.",
        )

    org_tz = get_organization_timezone()
    ref_utc = reference_time or datetime.now(timezone.utc)
    ref_local = ref_utc.astimezone(org_tz)

    # 1. Parse target clause and source reference
    source_ref, target_clause = _split_reschedule_prompt(prompt, ref_local)

    if not target_clause and selected_start_at is None:
        return ScheduledMeetingResult(
            status="needs_clarification",
            clarifying_question="What time would you like to move the meeting to?",
        )

    # 2. Find target meeting
    target_meeting, candidates, clarification = find_target_meeting_to_reschedule(
        db=db,
        source_ref=source_ref,
        requester_id=requester_id,
        org_tz=org_tz,
        ref_local=ref_local,
    )

    if clarification:
        if candidates:
            return ScheduledMeetingResult(
                status="needs_clarification",
                clarifying_question=clarification,
            )
        return ScheduledMeetingResult(status="error", message=clarification)

    if target_meeting is None:
        return ScheduledMeetingResult(
            status="error",
            message="No matching meeting found to reschedule.",
        )

    # 3. Resolve new start and end time
    if selected_start_at is None:
        time_of_day = parse_time_of_day(target_clause)
        if time_of_day is None:
            return ScheduledMeetingResult(
                status="needs_clarification",
                clarifying_question=f"What time on that day should '{target_meeting.title}' start?",
            )

        target_date = parse_meeting_date(target_clause, ref_local)
        if target_date is None:
            # Preserve target meeting's local date
            target_date = _as_utc(target_meeting.start_at).astimezone(org_tz).date()

        hour, minute = time_of_day
        new_start_local = datetime.combine(target_date, time(hour, minute), tzinfo=org_tz)
        new_start_utc = new_start_local.astimezone(timezone.utc)
    else:
        new_start_utc = selected_start_at
        if new_start_utc.tzinfo is None:
            new_start_utc = new_start_utc.replace(tzinfo=timezone.utc)
        else:
            new_start_utc = new_start_utc.astimezone(timezone.utc)
        new_start_local = new_start_utc.astimezone(org_tz)

    # Duration: preserve original meeting duration unless user explicitly specifies
    duration_match = re.search(
        r"\b(?:for\s+)?(\d+)\s*(hour|hr|minute|min)s?\b", prompt, re.IGNORECASE
    )
    if duration_match:
        duration_minutes = parse_duration_minutes(prompt)
    else:
        duration_minutes = max(
            1,
            int((target_meeting.end_at - target_meeting.start_at).total_seconds() / 60),
        )
    new_end_utc = new_start_utc + timedelta(minutes=duration_minutes)

    # 4. Resolve Attendees
    participants = (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.meeting_id == target_meeting.id)
        .all()
    )
    participant_user_ids = [p.user_id for p in participants if p.user_id]
    employees = (
        db.query(Employee)
        .filter(Employee.id.in_(participant_user_ids))
        .all()
        if participant_user_ids
        else []
    )
    employees_by_email = {
        emp.email.lower(): emp for emp in db.query(Employee).all() if emp.email
    }

    attendee_emails = list(
        dict.fromkeys(
            [p.email.lower() for p in participants if p.email]
            + [emp.email.lower() for emp in employees if emp.email]
        )
    )
    organizer_emp = (
        db.query(Employee).filter(Employee.id == target_meeting.organizer_id).first()
    )
    if (
        organizer_emp
        and organizer_emp.email
        and organizer_emp.email.lower() not in attendee_emails
    ):
        attendee_emails.append(organizer_emp.email.lower())

    attendee_count = len(attendee_emails)

    # 5. Check Working Hours
    if not _fits_attendee_working_hours(
        new_start_utc,
        new_end_utc,
        attendee_emails,
        employees_by_email,
        org_tz,
        default_for_unknown_attendees=True,
    ):
        alternatives = find_alternative_slots(
            db,
            new_start_utc,
            duration_minutes,
            attendee_emails,
            exclude_meeting_id=target_meeting.id,
        )
        working_hours_conflicts = [
            FreeBusyConflict(
                attendee_email=email,
                attendee_name=(
                    employees_by_email[email].name if email in employees_by_email else None
                ),
                conflicting_event_title="Outside working hours",
                conflicting_start_at=new_start_utc,
                conflicting_end_at=new_end_utc,
            )
            for email in attendee_emails
        ]
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=working_hours_conflicts,
            alternative_slots=alternatives,
            message="The requested slot is outside one or more attendees' working hours. Please choose another slot.",
        )

    # 6. Check Calendar Availability (excluding this meeting from its own conflict check)
    provider = get_calendar_provider()
    try:
        availability = provider.check_availability(
            db=db,
            attendee_emails=attendee_emails,
            start_at=new_start_utc,
            end_at=new_end_utc,
            exclude_meeting_id=target_meeting.id,
        )
    except Exception:
        logger.exception("Failed to check availability with calendar provider.")
        return ScheduledMeetingResult(
            status="error",
            message="The configured calendar provider is unavailable; meeting was not rescheduled.",
        )

    if availability.has_conflicts:
        conflict_descriptions = [
            f"{c.attendee_name or c.attendee_email} is busy with '{c.conflicting_event_title}'"
            for c in availability.conflicts
        ]
        alternatives = find_alternative_slots(
            db,
            new_start_utc,
            duration_minutes,
            attendee_emails,
            provider=provider,
            exclude_meeting_id=target_meeting.id,
        )
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=availability.conflicts,
            alternative_slots=alternatives,
            message=(
                f"Cannot reschedule meeting due to conflicts: {', '.join(conflict_descriptions)}. "
                "Please choose another time slot."
            ),
        )

    # 7. Authorization & Approval Check (Criteria 3, 4, 5)
    is_authorized = is_authorized_rescheduler(db, target_meeting, requester_id)
    requires_approval = (not is_authorized) or (attendee_count > 20)

    if requires_approval:
        reschedule_req = MeetingRescheduleRequest(
            id=str(uuid.uuid4()),
            meeting_id=target_meeting.id,
            requested_by=requester_id,
            new_start_at=_to_naive_utc(new_start_utc),
            new_end_at=_to_naive_utc(new_end_utc),
            reason=prompt,
            status=RESCHEDULE_STATUS_PENDING,
        )
        db.add(reschedule_req)
        db.commit()
        db.refresh(reschedule_req)

        reason_note = (
            "Meetings with more than 20 attendees require organizer approval."
            if attendee_count > 20
            else "Only the organizer or an authorized delegate can reschedule directly. A request has been sent for approval."
        )
        new_time_str = new_start_local.strftime("%A, %B %d at %I:%M %p")
        return ScheduledMeetingResult(
            status="pending_approval",
            message=(
                f"Reschedule request submitted for '{target_meeting.title}' to {new_time_str}. "
                f"{reason_note} (Approval Request ID: {reschedule_req.id})"
            ),
            reschedule_request_id=reschedule_req.id,
        )

    # 8. Authorized Direct Reschedule via Calendar Provider (Criteria 3 & 7)
    previous_start = target_meeting.start_at
    previous_end = target_meeting.end_at
    try:
        updated_event = provider.update_event(
            db=db,
            event_id=target_meeting.id,
            start_at=new_start_utc,
            end_at=new_end_utc,
        )
    except CalendarConflictError as exc:
        alternatives = find_alternative_slots(
            db,
            new_start_utc,
            duration_minutes,
            attendee_emails,
            provider=provider,
            exclude_meeting_id=target_meeting.id,
        )
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=exc.conflicts,
            alternative_slots=alternatives,
            message=str(exc),
        )
    except Exception as exc:
        logger.exception("Failed to update event in calendar provider.")
        return ScheduledMeetingResult(
            status="error",
            message=f"Failed to reschedule meeting: {str(exc)}",
        )

    # 9. Send Notifications to Attendees (Criterion 6)
    organizer_name = organizer_emp.name if organizer_emp else "Meeting Organizer"
    invitations_sent, invitation_failures = send_meeting_rescheduled_notifications(
        meeting=updated_event,
        organizer_name=organizer_name,
        attendees=employees,
        timezone_name=str(org_tz),
        previous_start=previous_start,
        previous_end=previous_end,
    )

    attendee_details = [
        {"id": emp.id, "name": emp.name, "email": emp.email, "team": emp.team}
        for emp in employees
    ]

    start_local_formatted = new_start_local.strftime("%A, %B %d at %I:%M %p")
    confirmation_message = (
        f"Rescheduled '{updated_event.title}' to {start_local_formatted} ({duration_minutes} minutes) "
        f"with {attendee_count} attendee(s). Join link: {updated_event.join_url}"
    )

    if invitation_failures:
        failed_recipients = ", ".join(invitation_failures)
        return ScheduledMeetingResult(
            status="error",
            meeting=updated_event,
            message=(
                f"Meeting '{updated_event.title}' was rescheduled, but notifications could not be "
                f"delivered to: {failed_recipients}. Join link: {updated_event.join_url}"
            ),
            resolved_attendees=attendee_details,
            invitations_sent=invitations_sent,
            invitation_failures=invitation_failures,
        )

    return ScheduledMeetingResult(
        status="Completed",
        meeting=updated_event,
        message=confirmation_message,
        resolved_attendees=attendee_details,
        invitations_sent=invitations_sent,
        invitation_failures=[],
    )


def decide_reschedule_request(
    db: Session,
    request_id: str,
    decision: Literal["approve", "reject"],
    decider_id: str,
    decision_note: str | None = None,
) -> tuple[MeetingRescheduleRequest, CalendarEvent | None, str]:
    """Approve or reject a meeting reschedule request (Story 5.3)."""
    req = (
        db.query(MeetingRescheduleRequest)
        .filter(MeetingRescheduleRequest.id == request_id)
        .first()
    )
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reschedule request '{request_id}' not found.",
        )

    if req.status != RESCHEDULE_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Reschedule request '{request_id}' has already been decided ({req.status}).",
        )

    meeting = db.query(Meeting).filter(Meeting.id == req.meeting_id).first()
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting '{req.meeting_id}' not found.",
        )

    # Authorization: Only the meeting's organizer (or an authorized delegate) can decide
    if not is_authorized_rescheduler(db, meeting, decider_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer or an authorized delegate can decide this reschedule request.",
        )

    now = datetime.now(timezone.utc)
    if decision == "reject":
        req.status = RESCHEDULE_STATUS_REJECTED
        req.decided_by = decider_id
        req.decided_at = now
        req.decision_note = decision_note
        db.commit()
        db.refresh(req)
        return req, None, f"Reschedule request for '{meeting.title}' was rejected."

    # Decision == "approve":
    provider = get_calendar_provider()
    org_tz = get_organization_timezone()

    # Collect attendee emails for conflict check
    participants = (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.meeting_id == meeting.id)
        .all()
    )
    attendee_emails = list(dict.fromkeys(p.email.lower() for p in participants if p.email))
    organizer_emp = db.query(Employee).filter(Employee.id == meeting.organizer_id).first()
    if organizer_emp and organizer_emp.email and organizer_emp.email.lower() not in attendee_emails:
        attendee_emails.append(organizer_emp.email.lower())

    previous_start = meeting.start_at
    previous_end = meeting.end_at

    try:
        updated_event = provider.update_event(
            db=db,
            event_id=meeting.id,
            start_at=req.new_start_at,
            end_at=req.new_end_at,
        )
    except CalendarConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve reschedule request due to conflict: {str(exc)}",
        )

    req.status = RESCHEDULE_STATUS_APPROVED
    req.decided_by = decider_id
    req.decided_at = now
    req.decision_note = decision_note
    db.commit()
    db.refresh(req)

    # Send notifications to attendees
    participant_user_ids = [p.user_id for p in participants if p.user_id]
    employees = (
        db.query(Employee).filter(Employee.id.in_(participant_user_ids)).all()
        if participant_user_ids
        else []
    )
    organizer_name = organizer_emp.name if organizer_emp else "Meeting Organizer"
    send_meeting_rescheduled_notifications(
        meeting=updated_event,
        organizer_name=organizer_name,
        attendees=employees,
        timezone_name=str(org_tz),
        previous_start=previous_start,
        previous_end=previous_end,
    )

    return (
        req,
        updated_event,
        f"Reschedule request approved. '{meeting.title}' has been moved to the new time.",
    )
