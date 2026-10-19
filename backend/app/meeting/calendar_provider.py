"""Calendar provider interface and local development provider (Story 5.1).

Defines the pluggable CalendarProvider abstraction and a local SQLite-backed
calendar provider for local development and testing.

External providers (such as Google Calendar or Microsoft Graph) require external
API credentials and configuration. When unconfigured, the local provider serves
as the development implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from typing import Literal
import uuid

from sqlalchemy.orm import Session

from app.meeting.models import Employee, Meeting, MeetingParticipant
from app.meeting.schemas import MeetingCreate, ParticipantCreate
from app.meeting.service import create_meeting

def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize datetime to naive UTC for consistent SQLite datetime comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class CalendarEvent:
    id: str
    title: str
    start_at: datetime
    end_at: datetime
    join_url: str | None = None
    description: str | None = None
    organizer_id: str | None = None
    organizer_email: str | None = None
    attendee_emails: list[str] = field(default_factory=list)
    status: str = "confirmed"
    provider_name: str = "local"


@dataclass
class FreeBusyConflict:
    attendee_email: str
    conflicting_event_title: str
    conflicting_start_at: datetime
    conflicting_end_at: datetime
    attendee_name: str | None = None


@dataclass
class FreeBusyResult:
    has_conflicts: bool
    conflicts: list[FreeBusyConflict] = field(default_factory=list)


class CalendarProviderError(Exception):
    """Base error for calendar provider operations."""


class CalendarConflictError(CalendarProviderError):
    """One or more attendees have conflicting events (HTTP 409)."""

    def __init__(self, message: str, conflicts: list[FreeBusyConflict]):
        super().__init__(message)
        self.conflicts = conflicts


class CalendarProvider(ABC):
    """Abstract calendar provider interface for scheduling and availability checks."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the calendar provider."""

    @abstractmethod
    def check_availability(
        self,
        db: Session,
        attendee_emails: list[str],
        start_at: datetime,
        end_at: datetime,
        exclude_meeting_id: str | None = None,
    ) -> FreeBusyResult:
        """Check whether any of the attendees have conflicting commitments during the requested window."""

    @abstractmethod
    def create_event(
        self,
        db: Session,
        title: str,
        start_at: datetime,
        end_at: datetime,
        organizer_id: str,
        attendee_emails: list[str],
        agenda: str | None = None,
    ) -> CalendarEvent:
        """Create a scheduled event in the calendar provider."""

    @abstractmethod
    def update_event(
        self,
        db: Session,
        event_id: str,
        start_at: datetime,
        end_at: datetime,
        title: str | None = None,
        agenda: str | None = None,
    ) -> CalendarEvent:
        """Update an existing scheduled event in the calendar provider."""

    @abstractmethod
    def cancel_event(self, db: Session, event_id: str) -> CalendarEvent:
        """Mark an event as cancelled in the calendar provider."""

    @abstractmethod
    def add_attendee(
        self,
        db: Session,
        event_id: str,
        user_id: str | None,
        email: str,
    ) -> CalendarEvent:
        """Add one attendee to an existing event."""

    @abstractmethod
    def remove_attendee(
        self,
        db: Session,
        event_id: str,
        participant_id: str,
    ) -> CalendarEvent:
        """Remove one attendee from an existing event."""

    @abstractmethod
    def get_event(
        self,
        db: Session,
        event_id: str,
    ) -> CalendarEvent | None:
        """Fetch an event by its unique ID from the provider."""


class LocalCalendarProvider(CalendarProvider):
    """Local SQLite-backed calendar provider used for development and testing.

    Stores scheduled meetings in the application's database and checks
    availability against existing meetings.
    """

    @property
    def provider_name(self) -> str:
        return "local"

    def check_availability(
        self,
        db: Session,
        attendee_emails: list[str],
        start_at: datetime,
        end_at: datetime,
        exclude_meeting_id: str | None = None,
    ) -> FreeBusyResult:
        search_start = _to_naive_utc(start_at)
        search_end = _to_naive_utc(end_at)

        conflicts: list[FreeBusyConflict] = []
        normalized_emails = {e.strip().lower() for e in attendee_emails if e and e.strip()}
        if not normalized_emails:
            return FreeBusyResult(has_conflicts=False, conflicts=[])

        # Map emails to employee records where available
        employees_by_email = {
            email.lower(): (name, employee_id)
            for employee_id, name, email in db.query(
                Employee.id, Employee.name, Employee.email
            ).filter(Employee.email.in_(normalized_emails)).all()
        }

        # Find overlapping scheduled or in-progress meetings
        query = db.query(Meeting).filter(
            Meeting.status.in_(["scheduled", "instant", "in_progress"]),
            Meeting.start_at < search_end,
            Meeting.end_at > search_start,
        )
        if exclude_meeting_id:
            query = query.filter(Meeting.id != exclude_meeting_id)
        overlapping_meetings = query.all()

        for meeting in overlapping_meetings:
            # Check participants of this overlapping meeting
            participants = (
                db.query(MeetingParticipant)
                .filter(MeetingParticipant.meeting_id == meeting.id)
                .all()
            )
            meeting_emails = {p.email.lower() for p in participants if p.email}
            participant_user_ids = [p.user_id for p in participants if p.user_id]
            if participant_user_ids:
                meeting_emails.update(
                    employee.email.lower()
                    for employee in db.query(Employee)
                    .filter(Employee.id.in_(participant_user_ids))
                    .all()
                    if employee.email
                )
            # Also check if organizer matches an employee with a checked email
            organizer_emp = db.query(Employee).filter(Employee.id == meeting.organizer_id).first()
            if organizer_emp:
                meeting_emails.add(organizer_emp.email.lower())

            for email in normalized_emails:
                if email in meeting_emails:
                    employee_details = employees_by_email.get(email)
                    conflicts.append(
                        FreeBusyConflict(
                            attendee_email=email,
                            attendee_name=employee_details[0] if employee_details else None,
                            conflicting_event_title=meeting.title,
                            conflicting_start_at=meeting.start_at,
                            conflicting_end_at=meeting.end_at,
                        )
                    )

        return FreeBusyResult(
            has_conflicts=len(conflicts) > 0,
            conflicts=conflicts,
        )

    def create_event(
        self,
        db: Session,
        title: str,
        start_at: datetime,
        end_at: datetime,
        organizer_id: str,
        attendee_emails: list[str],
        agenda: str | None = None,
    ) -> CalendarEvent:
        normalized_emails = list(dict.fromkeys(
            e.strip().lower() for e in attendee_emails if e and e.strip()
        ))

        # Check availability before creating to prevent double-booking
        availability = self.check_availability(db, normalized_emails, start_at, end_at)
        if availability.has_conflicts:
            conflict_names = [
                c.attendee_name or c.attendee_email for c in availability.conflicts
            ]
            raise CalendarConflictError(
                f"Scheduling conflict detected for {', '.join(conflict_names)}.",
                conflicts=availability.conflicts,
            )

        # Resolve employee IDs where available
        emp_map: dict[str, Employee] = {
            emp.email.lower(): emp
            for emp in db.query(Employee).filter(Employee.email.in_(normalized_emails)).all()
        }

        participants = [
            ParticipantCreate(
                user_id=emp_map[email].id if email in emp_map else None,
                email=email,
            )
            for email in normalized_emails
        ]

        # Use naive UTC for database persistence to match existing convention
        persisted_start = _to_naive_utc(start_at)
        persisted_end = _to_naive_utc(end_at)

        meeting_data = MeetingCreate(
            title=title,
            agenda=agenda,
            meeting_type="scheduled",
            start_at=persisted_start,
            end_at=persisted_end,
            participants=participants,
        )

        meeting = create_meeting(
            db=db,
            meeting_data=meeting_data,
            organizer_id=organizer_id,
        )

        # Requirement 9: Read back created event from provider to confirm persistence
        created_event = self.get_event(db, meeting.id)
        if created_event is None:
            raise CalendarProviderError(
                f"Failed to verify event creation in calendar provider for meeting {meeting.id}."
            )

        return created_event

    def update_event(
        self,
        db: Session,
        event_id: str,
        start_at: datetime,
        end_at: datetime,
        title: str | None = None,
        agenda: str | None = None,
    ) -> CalendarEvent:
        meeting = db.query(Meeting).filter(Meeting.id == event_id).first()
        if meeting is None:
            raise CalendarProviderError(f"Meeting with ID '{event_id}' not found.")

        # Collect attendee emails for conflict check
        participants = (
            db.query(MeetingParticipant)
            .filter(MeetingParticipant.meeting_id == meeting.id)
            .all()
        )
        attendee_emails = [p.email for p in participants if p.email]
        organizer_emp = db.query(Employee).filter(Employee.id == meeting.organizer_id).first()
        if organizer_emp and organizer_emp.email:
            attendee_emails.append(organizer_emp.email)

        normalized_emails = list(dict.fromkeys(
            e.strip().lower() for e in attendee_emails if e and e.strip()
        ))

        # Check availability, excluding this meeting from its own conflict check
        availability = self.check_availability(
            db=db,
            attendee_emails=normalized_emails,
            start_at=start_at,
            end_at=end_at,
            exclude_meeting_id=meeting.id,
        )
        if availability.has_conflicts:
            conflict_names = [
                c.attendee_name or c.attendee_email for c in availability.conflicts
            ]
            raise CalendarConflictError(
                f"Scheduling conflict detected for {', '.join(conflict_names)}.",
                conflicts=availability.conflicts,
            )

        persisted_start = _to_naive_utc(start_at)
        persisted_end = _to_naive_utc(end_at)

        meeting.start_at = persisted_start
        meeting.end_at = persisted_end
        if title is not None:
            meeting.title = title
        if agenda is not None:
            meeting.agenda = agenda

        db.commit()
        db.refresh(meeting)

        # Requirement 7: Read the event back from the calendar provider and verify the new start and end times
        updated_event = self.get_event(db, meeting.id)
        if updated_event is None:
            raise CalendarProviderError(
                f"Failed to verify event update in calendar provider for meeting {meeting.id}."
            )

        readback_start = _to_naive_utc(updated_event.start_at)
        readback_end = _to_naive_utc(updated_event.end_at)
        if readback_start != persisted_start or readback_end != persisted_end:
            raise CalendarProviderError(
                f"Provider read-back verification failed for meeting {meeting.id}: timestamps do not match."
            )

        return updated_event

    def cancel_event(self, db: Session, event_id: str) -> CalendarEvent:
        meeting = db.query(Meeting).filter(Meeting.id == event_id).first()
        if meeting is None:
            raise CalendarProviderError(f"Meeting with ID '{event_id}' not found.")
        meeting.status = "cancelled"
        db.flush()
        event = self.get_event(db, event_id)
        if event is None:
            raise CalendarProviderError(f"Failed to verify cancellation for meeting {event_id}.")
        return event

    def add_attendee(
        self,
        db: Session,
        event_id: str,
        user_id: str | None,
        email: str,
    ) -> CalendarEvent:
        meeting = db.query(Meeting).filter(Meeting.id == event_id).first()
        if meeting is None:
            raise CalendarProviderError(f"Meeting with ID '{event_id}' not found.")
        normalized_email = email.strip().lower()
        participants = (
            db.query(MeetingParticipant)
            .filter(MeetingParticipant.meeting_id == event_id)
            .all()
        )
        if not any(
            (participant.email and participant.email.strip().lower() == normalized_email)
            or (user_id and participant.user_id == user_id)
            for participant in participants
        ):
            employee = (
                db.query(Employee)
                .filter(Employee.email.ilike(normalized_email))
                .first()
            )
            db.add(
                MeetingParticipant(
                    id=str(uuid.uuid4()),
                    meeting_id=event_id,
                    user_id=user_id or (employee.id if employee else None),
                    email=normalized_email,
                    role="participant",
                )
            )
            db.flush()
        event = self.get_event(db, event_id)
        if event is None:
            raise CalendarProviderError(f"Failed to verify attendee update for meeting {event_id}.")
        return event

    def remove_attendee(
        self,
        db: Session,
        event_id: str,
        participant_id: str,
    ) -> CalendarEvent:
        participant = (
            db.query(MeetingParticipant)
            .filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == event_id,
            )
            .first()
        )
        if participant is None:
            raise CalendarProviderError(f"Participant '{participant_id}' not found in meeting '{event_id}'.")
        db.delete(participant)
        db.flush()
        event = self.get_event(db, event_id)
        if event is None:
            raise CalendarProviderError(f"Failed to verify attendee update for meeting {event_id}.")
        return event

    def get_event(
        self,
        db: Session,
        event_id: str,
    ) -> CalendarEvent | None:
        meeting = db.query(Meeting).filter(Meeting.id == event_id).first()
        if meeting is None:
            return None

        participants = (
            db.query(MeetingParticipant)
            .filter(MeetingParticipant.meeting_id == meeting.id)
            .all()
        )
        attendee_emails = [p.email for p in participants if p.email]

        return CalendarEvent(
            id=meeting.id,
            title=meeting.title,
            description=meeting.agenda,
            start_at=meeting.start_at,
            end_at=meeting.end_at,
            join_url=meeting.join_url,
            organizer_id=meeting.organizer_id,
            attendee_emails=attendee_emails,
            status=meeting.status,
            provider_name=self.provider_name,
        )


def get_calendar_provider() -> CalendarProvider:
    """Factory to retrieve the configured calendar provider.

    Defaults to LocalCalendarProvider for local development and testing.
    """
    provider_type = os.getenv("CALENDAR_PROVIDER", "local").strip().lower()
    if provider_type == "local":
        return LocalCalendarProvider()

    raise CalendarProviderError(
        f"Calendar provider '{provider_type}' is not configured. "
        "Only CALENDAR_PROVIDER=local is currently available."
    )
