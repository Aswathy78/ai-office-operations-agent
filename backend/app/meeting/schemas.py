from datetime import datetime
from typing import Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ParticipantCreate(BaseModel):
    user_id: str | None = None
    email: str | None = None


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
            if self.end_at <= self.start_at:
                raise ValueError("End time must be after start time")

        return self


class MeetingResponse(BaseModel):
    id: str
    title: str
    agenda: str | None
    start_at: datetime
    end_at: datetime
    join_url: str
    status: str
    organizer_id: str | None = None


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


class MeetingTranscriptResponse(BaseModel):
    id: str
    meeting_id: str
    recording_id: str
    status: str
    language: str
    transcript_text: str | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    error_message: str | None = None
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
    error_message: str | None = None


class MeetingActionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_name: str | None = None
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
    error_message: str | None = None
    action_items: list[MeetingActionItemResponse] = Field(default_factory=list)


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
    error_message: str | None = None
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