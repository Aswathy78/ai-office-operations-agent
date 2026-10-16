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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.email_draft_service import (
    _active_employees,
    _group_members,
    detect_groups,
    detect_named_employees,
)
from app.meeting.calendar_provider import (
    CalendarConflictError,
    CalendarEvent,
    FreeBusyConflict,
    get_calendar_provider,
)
from app.meeting.employee_matching import match_employee_name
from app.meeting.models import Employee, Meeting
from app.task_service import deliver_email

logger = logging.getLogger(__name__)

DEFAULT_DURATION_MINUTES = 30
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ScheduledMeetingResult:
    status: Literal["Completed", "needs_clarification", "conflict", "error"]
    meeting: CalendarEvent | None = None
    clarifying_question: str | None = None
    conflicts: list[FreeBusyConflict] = field(default_factory=list)
    message: str | None = None
    resolved_attendees: list[dict] = field(default_factory=list)
    invitations_sent: list[str] = field(default_factory=list)
    invitation_failures: list[str] = field(default_factory=list)


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
    # 12-hour format: '10 AM', '10:30 pm', 'at 9am'
    match12 = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
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
        att.email.lower(): f"{att.name} <{att.email}>" for att in attendees if att.email
    }
    if raw_emails:
        for email in raw_emails:
            e_lower = email.strip().lower()
            if e_lower and e_lower not in recipient_map:
                recipient_map[e_lower] = email.strip()

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


def schedule_meeting_from_natural_language(
    db: Session,
    prompt: str,
    organizer_id: str,
    reference_time: datetime | None = None,
) -> ScheduledMeetingResult:
    """Core entrypoint for Story 5.1: Natural-Language Meeting Scheduling."""
    prompt = prompt.strip()
    if not prompt:
        return ScheduledMeetingResult(
            status="error",
            message="Please provide meeting scheduling details.",
        )

    # 1. Resolve Timezone and Local Reference Time
    org_tz = get_organization_timezone()
    ref_utc = reference_time or datetime.now(timezone.utc)
    ref_local = ref_utc.astimezone(org_tz)

    # 2. Parse Date, Time, Duration, and Title
    target_date = parse_meeting_date(prompt, ref_local)
    time_of_day = parse_time_of_day(prompt)

    if target_date is None:
        # Default to tomorrow if not stated, or ask if neither date nor time could be found
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
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=availability.conflicts,
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
        return ScheduledMeetingResult(
            status="conflict",
            conflicts=exc.conflicts,
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
