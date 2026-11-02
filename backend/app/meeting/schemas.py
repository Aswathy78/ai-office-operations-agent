from datetime import datetime, timezone
import re
from typing import Literal
from app.plain_errors import PlainError
from app.time_utils import UtcDateTime
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

_PARTICIPANT_EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}$",
    re.IGNORECASE,
)


class ParticipantCreate(BaseModel):
    user_id: str | None = None
    email: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_and_validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Participant email must be a valid email address.")
        email = value.strip().casefold()
        if not email:
            return None
        if len(email) > 254 or not _PARTICIPANT_EMAIL_PATTERN.fullmatch(email):
            raise ValueError("Participant email must be a valid email address.")
        return email


class MeetingCreate(BaseModel):
    title: str = Field(..., min_length=1)
    agenda: str | None = None
    meeting_type: str = Field(default="scheduled")
    # Optional: omitted for instant meetings, required for scheduled meetings
    start_at: datetime | None = None
    end_at: datetime | None = None
    participants: list[ParticipantCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_meeting(self):
        if self.meeting_type not in ["instant", "scheduled"]:
            raise ValueError("Meeting type must be instant or scheduled")

        if self.meeting_type == "scheduled":
            if self.start_at is None or self.end_at is None:
                raise ValueError("start_at and end_at are required for scheduled meetings")
            start_at = self._as_utc_naive(self.start_at)
            end_at = self._as_utc_naive(self.end_at)
            if end_at <= start_at:
                raise ValueError("End time must be after start time")
            self.start_at = start_at
            self.end_at = end_at

        participants_by_email: dict[str, int] = {}
        unique_participants: list[ParticipantCreate] = []
        for participant in self.participants:
            if participant.email is None:
                unique_participants.append(participant)
                continue

            existing_index = participants_by_email.get(participant.email)
            if existing_index is None:
                participants_by_email[participant.email] = len(unique_participants)
                unique_participants.append(participant)
                continue

            existing = unique_participants[existing_index]
            if (
                existing.user_id is not None
                and participant.user_id is not None
                and existing.user_id != participant.user_id
            ):
                raise ValueError(
                    "Duplicate participant emails cannot refer to different user IDs."
                )
            if existing.user_id is None and participant.user_id is not None:
                unique_participants[existing_index] = participant

        self.participants = unique_participants
        return self

    @staticmethod
    def _as_utc_naive(value: datetime) -> datetime:
        """Persist offset-aware values as UTC; treat naive API values as UTC."""
        if value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None)


class MeetingResponse(BaseModel):
    id: str
    title: str
    agenda: str | None
    start_at: datetime
    end_at: datetime
    join_url: str
    status: str
    organizer_id: str | None = None
    timezone: str


class MeetingCreationResponse(MeetingResponse):
    invitations_sent: list[str] = Field(default_factory=list)
    invitation_failures: list[str] = Field(default_factory=list)


class MeetingListResponse(MeetingResponse):
    can_manage: bool = False


class MeetingCalendarAttendeeResponse(BaseModel):
    employee_id: str | None = None
    name: str
    email: str


class MeetingCalendarEventResponse(BaseModel):
    id: str
    title: str
    start_at: datetime
    end_at: datetime
    join_url: str
    status: str
    organizer_id: str | None = None
    attendees: list[MeetingCalendarAttendeeResponse] = Field(default_factory=list)


class MeetingCalendarResponse(BaseModel):
    timezone: str
    meetings: list[MeetingCalendarEventResponse] = Field(default_factory=list)


class EmployeeResponse(BaseModel):
    id: str
    name: str
    email: str
    team: str | None = None


class MeetingValidationResponse(BaseModel):
    id: str
    title: str
    agenda: str | None = None
    start_at: datetime
    end_at: datetime
    status: str
    organizer_id: str
    is_valid: bool = True
    is_expired: bool = False


class AttendanceJoinRequest(BaseModel):
    participant_name: str = Field(..., min_length=1)
    participant_email: str | None = None
    user_id: str | None = None
    is_external_guest: bool = False
    guest_request_id: str | None = None


class AttendanceResponse(BaseModel):
    id: str
    meeting_id: str
    participant_name: str
    participant_email: str | None = None
    user_id: str | None = None
    is_external_guest: bool
    joined_at: datetime
    left_at: datetime | None = None


# -- Guest Lobby ---------------------------------------------------------------


class GuestJoinRequest(BaseModel):
    """Submitted by an external guest to enter the lobby."""

    guest_name: str = Field(..., min_length=1, max_length=255)
    guest_email: str | None = Field(default=None, max_length=255)


class GuestRequestResponse(BaseModel):
    """Returned after a guest submits a lobby request or for organizer listings."""

    id: str
    meeting_id: str
    guest_name: str
    guest_email: str | None = None
    status: str
    requested_at: datetime
    decided_at: datetime | None = None
    attendance_log_id: str | None = None


class GuestDecisionRequest(BaseModel):
    """Submitted by the organizer to admit or reject a guest."""

    decision: str = Field(
        ...,
        pattern="^(admit|reject)$",
        validation_alias=AliasChoices("decision", "action"),
    )


# -- Meeting Report ------------------------------------------------------------


class AttendanceReportRow(BaseModel):
    """One row of the organizer meeting report."""

    participant_name: str
    participant_email: str | None = None
    user_id: str | None = None
    is_external_guest: bool
    joined_at: datetime
    left_at: datetime | None = None


class MeetingReportResponse(BaseModel):
    """Meeting report returned to the organizer."""

    meeting_id: str
    title: str
    agenda: str | None = None
    start_at: datetime
    end_at: datetime
    organizer_id: str
    attendance: list[AttendanceReportRow]


class RecordingConsentRequest(BaseModel):
    attendance_id: str
    consent_status: Literal["consented", "declined"]


class RecordingConsentResponse(BaseModel):
    id: str
    meeting_id: str
    attendance_id: str
    consent_status: str
    consented_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

class TranscriptWord(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    word: str = Field(min_length=1)


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str
    flagged_for_correction: bool = False
    words: list[TranscriptWord] | None = None

    @model_serializer(mode="wrap")
    def _serialize_segment(self, handler):
        data = handler(self)
        if data.get("words") is None:
            data.pop("words", None)
        return data


class MeetingTranscriptResponse(BaseModel):
    id: str
    meeting_id: str
    recording_id: str
    status: str
    language: str
    transcript_text: str | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    error_message: PlainError = None
    created_at: datetime
    processing_started_at: datetime | None = None
    completed_at: datetime | None = None


class MeetingSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    meeting_id: str
    transcript_id: str
    status: Literal["pending", "processing", "completed", "failed"]
    overview: str | None = None
    decisions: list[str] | None = None
    discussion_topics: list[str] | None = None
    open_questions: list[str] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_message: PlainError = None


class ActionItemOwnerConfirmationRequest(BaseModel):
    employee_id: str


class ActionItemReviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["confirm", "reject", "edit", "save"] | None = None
    decision: Literal["confirm", "reject", "edit", "confirmed", "rejected"] | None = None
    review_status: Literal[
        "pending_review", "approval_required", "confirmed", "rejected", "approved"
    ] | None = None
    task_description: str | None = None
    owner_name: str | None = None
    confirmed_employee_id: str | None = None
    deadline_at: datetime | None = None
    deadline_text: str | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None


class MeetingActionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_name: str | None = None
    proposed_employee_id: str | None = None
    confirmed_employee_id: str | None = None
    employee_match_status: str | None = None
    ownership_status: str | None = None
    review_status: str = "pending_review"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    is_task_eligible: bool = False
    requires_approval: bool = False
    task_description: str
    deadline_at: datetime | None = None
    deadline_text: str | None = None
    priority: Literal["low", "medium", "high", "urgent"]
    confidence: float = Field(ge=0, le=1)
    meeting_id: str
    transcript_id: str
    source_segment_index: int = Field(ge=0)
    source_start_seconds: float = Field(ge=0)
    source_end_seconds: float = Field(ge=0)
    source_quote_start_seconds: float | None = Field(default=None, ge=0)
    source_quote_end_seconds: float | None = Field(default=None, ge=0)
    source_quote: str | None = None


class MeetingActionItemsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    meeting_id: str
    transcript_id: str
    status: Literal["pending", "processing", "completed", "failed"]
    created_at: datetime
    updated_at: datetime
    processing_started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: PlainError = None
    action_items: list[MeetingActionItemResponse] = Field(default_factory=list)


class TaskCalendarSyncResponse(BaseModel):
    """The task deadline's calendar sync (Story 5.5): exactly one status."""

    status: Literal["pending", "processing", "completed", "retrying", "failed"]
    provider: str
    pending_reason: str | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_attempt_at: UtcDateTime | None = None
    last_error: PlainError = None
    last_synced_at: UtcDateTime | None = None
    last_polled_at: UtcDateTime | None = None
    external_change_detected_at: UtcDateTime | None = None
    external_change_detail: str | None = None


class TaskResponse(BaseModel):
    id: str
    title: str
    assignee_id: str
    assignee_name: str
    creator_id: str
    source: str
    # Meeting provenance; None for tasks the AI assistant created directly (Story 6.5).
    source_action_item_id: str | None = None
    meeting_id: str | None = None
    meeting_title: str | None = None
    transcript_id: str | None = None
    source_segment_index: int | None = None
    source_timestamp_seconds: float | None = None
    due_at: datetime
    priority: Literal["low", "medium", "high", "urgent"]
    status: Literal["pending", "completed"]
    completed_at: datetime | None = None
    assignment_email_status: Literal["pending", "sent", "failed"]
    assignment_email_error: PlainError = None
    created_at: datetime
    assignee_email: str | None = None
    assignee_team: str | None = None
    updated_at: datetime | None = None
    calendar_sequence: int = 0
    pending_reassignment: "TaskReassignmentRequestResponse | None" = None
    # None until the sync scheduler has picked the task up.
    calendar_sync: TaskCalendarSyncResponse | None = None


TaskActorRole = Literal["employee", "manager", "hr_operations", "admin"]


class TaskStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "completed"]


class TaskReminderRunResponse(BaseModel):
    checked: int
    reminders_sent: int
    escalations_sent: int
    failed: int
    skipped_no_manager: int
    notification_ids: list[str]


class TaskReassignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_id: str = Field(..., min_length=1)
    reason: str | None = Field(default=None, max_length=1000)


class TaskDueDateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    due_at: datetime
    reason: str | None = Field(default=None, max_length=1000)


class TaskReassignmentDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


class TaskReassignmentRequestResponse(BaseModel):
    id: str
    task_id: str
    task_title: str
    from_assignee_id: str
    from_assignee_name: str
    to_assignee_id: str
    to_assignee_name: str
    to_assignee_team: str | None = None
    requested_by: str
    requester_role: str
    requester_team: str | None = None
    reason: str | None = None
    status: Literal["pending", "approved", "rejected"]
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime


class TaskAuditLogResponse(BaseModel):
    id: str
    task_id: str
    actor_id: str
    actor_role: str
    action: str
    outcome: Literal["applied", "pending_approval", "denied", "rejected"]
    detail: str
    created_at: datetime


class TaskNotificationResponse(BaseModel):
    id: str
    recipient_employee_id: str
    recipient_name: str
    kind: str
    subject: str
    status: Literal["pending", "sent", "failed"]
    error: PlainError = None
    created_at: datetime


class TaskChangeResponse(BaseModel):
    outcome: Literal["applied", "pending_approval"]
    message: str
    task: TaskResponse
    reassignment_request: TaskReassignmentRequestResponse | None = None
    notifications: list[TaskNotificationResponse] = Field(default_factory=list)


class TranscriptSegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1)
    speaker: str | None = Field(default=None, min_length=1, max_length=255)


class MeetingTranscriptUpdateRequest(BaseModel):
    segments: list[TranscriptSegmentUpdate]


class TranscriptCorrectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    meeting_id: str
    transcript_id: str
    segment_index: int
    original_text: str
    corrected_text: str
    original_speaker: str
    corrected_speaker: str
    editor_id: str
    created_at: datetime


class MeetingRecordingResponse(BaseModel):
    id: str
    meeting_id: str
    participant_id: str | None = None
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    retention_expires_at: datetime
    storage_uri: str | None = None
    error_message: PlainError = None
    # FR-8 participant-track fields
    participant_name: str | None = None
    connection_id: str | None = None


class MeetingRecordingUploadResponse(BaseModel):
    recording_id: str
    status: str
    storage_uri: str
    filename: str
    content_type: str
    size_bytes: int


# -- FR-8 Participant Track Registration ---------------------------------------


class ParticipantTrackItem(BaseModel):
    """Describes a single participant audio track produced by Stage 1 recording."""

    connection_id: str = Field(..., min_length=1, max_length=255)
    participant_name: str = Field(..., min_length=1, max_length=255)
    attendance_id: str | None = None


class ParticipantTracksRegisterRequest(BaseModel):
    """Registers multiple participant-track recording rows for a meeting."""

    tracks: list[ParticipantTrackItem] = Field(..., min_length=1)


class ParticipantTrackRegisterResponse(BaseModel):
    """Returned per registered participant track."""

    recording_id: str
    connection_id: str
    participant_name: str
    status: str
    meeting_id: str


# -- Meeting Minutes (FR-19) ---------------------------------------------------


class MeetingMinutesRecipientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    minutes_id: str
    recipient_name: str
    recipient_email: str
    attendance_log_id: str | None = None
    delivery_status: Literal["pending", "sent", "failed"] = "pending"
    error_message: PlainError = None
    idempotency_key: str | None = None
    sent_at: datetime | None = None
    created_at: datetime


class MeetingMinutesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    meeting_id: str
    summary_id: str | None = None
    status: Literal["draft", "pending_approval", "approved", "sent", "failed"]
    policy: Literal["manual_approval", "auto_send"]
    approved_by: str | None = None
    approved_at: datetime | None = None
    content_summary: str | None = None
    content_decisions: list[str] | None = None
    content_action_items: list[dict[str, object]] | None = None
    error_message: PlainError = None
    created_at: datetime
    updated_at: datetime
    recipients: list[MeetingMinutesRecipientResponse] = Field(default_factory=list)


class MeetingMinutesApproveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    notes: str | None = None


class MeetingMinutesActionItemPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    task_description: str
    owner_name: str | None = None
    owner_email: str | None = None
    deadline_text: str | None = None
    deadline_at: str | None = None
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class MeetingMinutesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_summary: str | None = None
    content_decisions: list[str] | None = None
    content_action_items: list[MeetingMinutesActionItemPayload] | None = None


class MeetingMinutesDeliveryResponse(BaseModel):
    """Summary of a minutes delivery (send) attempt."""

    model_config = ConfigDict(from_attributes=True)

    minutes_id: str
    meeting_id: str
    total: int
    sent: int
    failed: int
    pending: int
    recipients: list[MeetingMinutesRecipientResponse] = Field(default_factory=list)


class NaturalLanguageScheduleRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    organizer_id: str | None = None


class ScheduleConfirmationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    start_at: datetime
    organizer_id: str | None = None


class ConflictDetail(BaseModel):
    attendee_email: str
    conflicting_event_title: str
    conflicting_start_at: datetime
    conflicting_end_at: datetime
    attendee_name: str | None = None


class AlternativeSlotDetail(BaseModel):
    start_at: datetime
    end_at: datetime


class NaturalLanguageScheduleResponse(BaseModel):
    status: Literal["Completed", "needs_clarification", "conflict", "error", "pending_approval"]
    meeting: MeetingResponse | None = None
    timezone: str
    clarifying_question: str | None = None
    conflicts: list[ConflictDetail] = Field(default_factory=list)
    alternative_slots: list[AlternativeSlotDetail] = Field(default_factory=list)
    message: str | None = None
    resolved_attendees: list[EmployeeResponse] = Field(default_factory=list)
    invitations_sent: list[str] = Field(default_factory=list)
    invitation_failures: list[str] = Field(default_factory=list)
    calendar_provider: str = "local"
    reschedule_request_id: str | None = None


class MeetingRescheduleRequestResponse(BaseModel):
    id: str
    meeting_id: str
    meeting_title: str
    requested_by: str
    requested_by_name: str | None = None
    new_start_at: datetime
    new_end_at: datetime
    reason: str | None = None
    status: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime
    attendee_count: int = 0


class RescheduleDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = None


class RescheduleDecisionResponse(BaseModel):
    request: MeetingRescheduleRequestResponse
    meeting: MeetingResponse | None = None
    message: str


class MeetingDelegateCreateRequest(BaseModel):
    delegate_id: str
    meeting_id: str | None = None


class MeetingDelegateResponse(BaseModel):
    id: str
    organizer_id: str
    delegate_id: str
    meeting_id: str | None = None
    created_at: datetime


class MeetingCancellationRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


class MeetingCancellationRequestResponse(BaseModel):
    id: str
    meeting_id: str
    meeting_title: str
    requested_by: str
    reason: str | None = None
    status: Literal["pending", "approved", "rejected"]
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime
    attendee_count: int


class MeetingCancellationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


class MeetingParticipantChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, min_length=1)
    email: str | None = Field(default=None, min_length=3, max_length=255)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_and_validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Participant email must be a valid email address.")
        email = value.strip().casefold()
        if not email:
            return None
        if len(email) > 254 or not _PARTICIPANT_EMAIL_PATTERN.fullmatch(email):
            raise ValueError("Participant email must be a valid email address.")
        return email

    @model_validator(mode="after")
    def require_participant_identity(self):
        if not self.user_id and not self.email:
            raise ValueError("Provide a user_id or email for the participant.")
        return self


class MeetingParticipantBulkChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emails: list[str] = Field(min_length=1, max_length=200)


class MeetingChangeResponse(BaseModel):
    status: Literal["applied", "pending_approval"]
    message: str
    meeting: MeetingResponse | None = None
    request_id: str | None = None
    notifications_sent: list[str] = Field(default_factory=list)
    notification_failures: list[str] = Field(default_factory=list)


class MeetingParticipantBulkChangeResponse(BaseModel):
    message: str
    added: list[str] = Field(default_factory=list)
    already_present: list[str] = Field(default_factory=list)
    duplicate_addresses: list[str] = Field(default_factory=list)
    invalid_addresses: list[str] = Field(default_factory=list)
    failed_addresses: list[str] = Field(default_factory=list)
    notifications_sent: list[str] = Field(default_factory=list)
    notification_failures: list[str] = Field(default_factory=list)


class MeetingCancellationDecisionResponse(BaseModel):
    request: MeetingCancellationRequestResponse
    meeting: MeetingResponse | None = None
    message: str
    notifications_sent: list[str] = Field(default_factory=list)
    notification_failures: list[str] = Field(default_factory=list)


class MeetingAuditLogResponse(BaseModel):
    id: str
    meeting_id: str
    actor_id: str
    action: str
    details: dict
    created_at: datetime


class MeetingParticipantResponse(BaseModel):
    id: str
    user_id: str | None = None
    name: str
    email: str | None = None
    role: str


TaskResponse.model_rebuild()
