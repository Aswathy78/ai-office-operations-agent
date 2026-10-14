from datetime import datetime, timezone
from datetime import timedelta
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Allowed values for MeetingGuestRequest.status
GUEST_STATUS_PENDING = "pending"
GUEST_STATUS_ADMITTED = "admitted"
GUEST_STATUS_REJECTED = "rejected"

RECORDING_STATUS_PENDING = "pending"
RECORDING_STATUS_RECORDING = "recording"
RECORDING_STATUS_PROCESSING = "processing"
RECORDING_STATUS_COMPLETED = "completed"
RECORDING_STATUS_FAILED = "failed"
RECORDING_STATUS_CANCELLED = "cancelled"
RECORDING_STATUSES = (
    RECORDING_STATUS_PENDING,
    RECORDING_STATUS_RECORDING,
    RECORDING_STATUS_PROCESSING,
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_FAILED,
    RECORDING_STATUS_CANCELLED,
)

RECORDING_CONSENT_STATUS_PENDING = "pending"
RECORDING_CONSENT_STATUS_CONSENTED = "consented"
RECORDING_CONSENT_STATUS_DECLINED = "declined"
RECORDING_CONSENT_STATUS_REVOKED = "revoked"
RECORDING_CONSENT_STATUSES = (
    RECORDING_CONSENT_STATUS_PENDING,
    RECORDING_CONSENT_STATUS_CONSENTED,
    RECORDING_CONSENT_STATUS_DECLINED,
    RECORDING_CONSENT_STATUS_REVOKED,
)

RECORDING_RETENTION_DAYS = 90

TRANSCRIPT_STATUS_PENDING = "pending"
TRANSCRIPT_STATUS_PROCESSING = "processing"
TRANSCRIPT_STATUS_COMPLETED = "completed"
TRANSCRIPT_STATUS_FAILED = "failed"
TRANSCRIPT_STATUSES = (
    TRANSCRIPT_STATUS_PENDING,
    TRANSCRIPT_STATUS_PROCESSING,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
)

SUMMARY_STATUS_PENDING = "pending"
SUMMARY_STATUS_PROCESSING = "processing"
SUMMARY_STATUS_COMPLETED = "completed"
SUMMARY_STATUS_FAILED = "failed"
SUMMARY_STATUSES = (
    SUMMARY_STATUS_PENDING,
    SUMMARY_STATUS_PROCESSING,
    SUMMARY_STATUS_COMPLETED,
    SUMMARY_STATUS_FAILED,
)

ACTION_ITEM_EXTRACTION_STATUS_PENDING = "pending"
ACTION_ITEM_EXTRACTION_STATUS_PROCESSING = "processing"
ACTION_ITEM_EXTRACTION_STATUS_COMPLETED = "completed"
ACTION_ITEM_EXTRACTION_STATUS_FAILED = "failed"
ACTION_ITEM_EXTRACTION_STATUSES = (
    ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    ACTION_ITEM_EXTRACTION_STATUS_PROCESSING,
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_EXTRACTION_STATUS_FAILED,
)

ACTION_ITEM_PRIORITY_LOW = "low"
ACTION_ITEM_PRIORITY_MEDIUM = "medium"
ACTION_ITEM_PRIORITY_HIGH = "high"
ACTION_ITEM_PRIORITY_URGENT = "urgent"
ACTION_ITEM_PRIORITIES = (
    ACTION_ITEM_PRIORITY_LOW,
    ACTION_ITEM_PRIORITY_MEDIUM,
    ACTION_ITEM_PRIORITY_HIGH,
    ACTION_ITEM_PRIORITY_URGENT,
)

TASK_STATUS_PENDING = "pending"
TASK_STATUSES = (TASK_STATUS_PENDING,)

TASK_REASSIGNMENT_STATUS_PENDING = "pending"
TASK_REASSIGNMENT_STATUS_APPROVED = "approved"
TASK_REASSIGNMENT_STATUS_REJECTED = "rejected"
TASK_REASSIGNMENT_STATUSES = (
    TASK_REASSIGNMENT_STATUS_PENDING,
    TASK_REASSIGNMENT_STATUS_APPROVED,
    TASK_REASSIGNMENT_STATUS_REJECTED,
)

TASK_AUDIT_OUTCOME_APPLIED = "applied"
TASK_AUDIT_OUTCOME_PENDING_APPROVAL = "pending_approval"
TASK_AUDIT_OUTCOME_DENIED = "denied"
TASK_AUDIT_OUTCOME_REJECTED = "rejected"
TASK_AUDIT_OUTCOMES = (
    TASK_AUDIT_OUTCOME_APPLIED,
    TASK_AUDIT_OUTCOME_PENDING_APPROVAL,
    TASK_AUDIT_OUTCOME_DENIED,
    TASK_AUDIT_OUTCOME_REJECTED,
)

EMPLOYEE_MATCH_STATUS_UNIQUE = "unique_match"
EMPLOYEE_MATCH_STATUS_AMBIGUOUS = "ambiguous_match"
EMPLOYEE_MATCH_STATUS_NO_MATCH = "no_match"
EMPLOYEE_MATCH_STATUS_MISSING_OWNER = "missing_owner"
EMPLOYEE_MATCH_STATUSES = (
    EMPLOYEE_MATCH_STATUS_UNIQUE,
    EMPLOYEE_MATCH_STATUS_AMBIGUOUS,
    EMPLOYEE_MATCH_STATUS_NO_MATCH,
    EMPLOYEE_MATCH_STATUS_MISSING_OWNER,
)

MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED = "unresolved"
MEETING_ACTION_ITEM_OWNERSHIP_STATUS_PROPOSED = "proposed"
MEETING_ACTION_ITEM_OWNERSHIP_STATUS_APPROVAL_REQUIRED = "approval_required"
MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED = "confirmed"
MEETING_ACTION_ITEM_OWNERSHIP_STATUSES = (
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_PROPOSED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_APPROVAL_REQUIRED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED,
)

ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW = "pending_review"
ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED = "approval_required"
ACTION_ITEM_REVIEW_STATUS_CONFIRMED = "confirmed"
ACTION_ITEM_REVIEW_STATUS_REJECTED = "rejected"
ACTION_ITEM_REVIEW_STATUSES = (
    ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    ACTION_ITEM_REVIEW_STATUS_REJECTED,
)

MINUTES_STATUS_DRAFT = "draft"
MINUTES_STATUS_PENDING_APPROVAL = "pending_approval"
MINUTES_STATUS_APPROVED = "approved"
MINUTES_STATUS_SENT = "sent"
MINUTES_STATUS_FAILED = "failed"
MINUTES_STATUSES = (
    MINUTES_STATUS_DRAFT,
    MINUTES_STATUS_PENDING_APPROVAL,
    MINUTES_STATUS_APPROVED,
    MINUTES_STATUS_SENT,
    MINUTES_STATUS_FAILED,
)

MINUTES_POLICY_MANUAL_APPROVAL = "manual_approval"
MINUTES_POLICY_AUTO_SEND = "auto_send"
MINUTES_POLICIES = (
    MINUTES_POLICY_MANUAL_APPROVAL,
    MINUTES_POLICY_AUTO_SEND,
)

MINUTES_DELIVERY_STATUS_PENDING = "pending"
MINUTES_DELIVERY_STATUS_SENT = "sent"
MINUTES_DELIVERY_STATUS_FAILED = "failed"
MINUTES_DELIVERY_STATUSES = (
    MINUTES_DELIVERY_STATUS_PENDING,
    MINUTES_DELIVERY_STATUS_SENT,
    MINUTES_DELIVERY_STATUS_FAILED,
)


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organizer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    agenda: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    join_url: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="scheduled")

    attendance_logs: Mapped[list["MeetingAttendanceLog"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )

    guest_requests: Mapped[list["MeetingGuestRequest"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )

    summaries: Mapped[list["MeetingSummary"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )

    action_item_extractions: Mapped[list["MeetingActionItemExtraction"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )

    minutes: Mapped[list["MeetingMinutes"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
    )


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="participant",
    )


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    team: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingAttendanceLog(Base):
    __tablename__ = "meeting_attendance_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id"),
        nullable=False,
        index=True,
    )
    participant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    participant_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_external_guest: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    meeting: Mapped["Meeting"] = relationship(
        back_populates="attendance_logs",
    )


MeetingAttendance = MeetingAttendanceLog


class MeetingGuestRequest(Base):
    """Tracks external guests waiting in the lobby for organizer admission."""

    __tablename__ = "meeting_guest_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id"),
        nullable=False,
        index=True,
    )
    guest_name: Mapped[str] = mapped_column(String(255), nullable=False)
    guest_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # pending | admitted | rejected
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=GUEST_STATUS_PENDING,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # attendance_log_id is populated when the guest is admitted and joins
    attendance_log_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meeting_attendance_logs.id"),
        nullable=True,
    )

    meeting: Mapped["Meeting"] = relationship(
        back_populates="guest_requests",
    )


class MeetingRecording(Base):
    __tablename__ = "meeting_recordings"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in RECORDING_STATUSES)})",
            name="ck_meeting_recordings_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id"),
        nullable=False,
        index=True,
    )
    # Optional participant association supports future individual audio tracks.
    participant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meeting_participants.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=RECORDING_STATUS_PENDING,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
        + timedelta(days=RECORDING_RETENTION_DAYS),
    )
    storage_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FR-8: participant-track identity (NULL for the full-meeting organizer recording)
    participant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class MeetingRecordingConsent(Base):
    __tablename__ = "meeting_recording_consents"
    __table_args__ = (
        CheckConstraint(
            f"consent_status IN ({', '.join(repr(status) for status in RECORDING_CONSENT_STATUSES)})",
            name="ck_meeting_recording_consents_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id"),
        nullable=False,
        index=True,
    )
    attendance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_attendance_logs.id"),
        nullable=False,
        index=True,
    )
    consent_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=RECORDING_CONSENT_STATUS_PENDING,
    )
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingTranscript(Base):
    __tablename__ = "meeting_transcripts"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in TRANSCRIPT_STATUSES)})",
            name="ck_meeting_transcripts_status",
        ),
        UniqueConstraint("recording_id", name="uq_meeting_transcripts_recording_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recording_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_recordings.id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TRANSCRIPT_STATUS_PENDING,
    )
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    segments: Mapped[list[dict[str, object]] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    summaries: Mapped[list["MeetingSummary"]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
    )

    action_item_extraction: Mapped["MeetingActionItemExtraction | None"] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        uselist=False,
    )


class MeetingSummary(Base):
    __tablename__ = "meeting_summaries"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in SUMMARY_STATUSES)})",
            name="ck_meeting_summaries_status",
        ),
        UniqueConstraint(
            "transcript_id",
            name="uq_meeting_summaries_transcript_id",
        ),
        Index("ix_meeting_summaries_meeting_id", "meeting_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SUMMARY_STATUS_PENDING,
    )
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    discussion_topics: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    open_questions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    meeting: Mapped["Meeting"] = relationship(back_populates="summaries")
    transcript: Mapped["MeetingTranscript"] = relationship(back_populates="summaries")


class MeetingActionItemExtraction(Base):
    __tablename__ = "meeting_action_item_extractions"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            f"({', '.join(repr(status) for status in ACTION_ITEM_EXTRACTION_STATUSES)})",
            name="ck_meeting_action_item_extractions_status",
        ),
        UniqueConstraint(
            "transcript_id",
            name="uq_meeting_action_item_extractions_transcript_id",
        ),
        Index("ix_meeting_action_item_extractions_meeting_id", "meeting_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    meeting: Mapped["Meeting"] = relationship(
        back_populates="action_item_extractions",
    )
    transcript: Mapped["MeetingTranscript"] = relationship(
        back_populates="action_item_extraction",
    )
    items: Mapped[list["MeetingActionItem"]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
    )


class MeetingActionItem(Base):
    __tablename__ = "meeting_action_items"
    __table_args__ = (
        CheckConstraint(
            f"priority IN ({', '.join(repr(priority) for priority in ACTION_ITEM_PRIORITIES)})",
            name="ck_meeting_action_items_priority",
        ),
        CheckConstraint(
            f"employee_match_status IN ({', '.join(repr(status) for status in EMPLOYEE_MATCH_STATUSES)}) OR employee_match_status IS NULL",
            name="ck_meeting_action_items_employee_match_status",
        ),
        CheckConstraint(
            f"ownership_status IN ({', '.join(repr(status) for status in MEETING_ACTION_ITEM_OWNERSHIP_STATUSES)})",
            name="ck_meeting_action_items_ownership_status",
        ),
        CheckConstraint(
            f"review_status IN ({', '.join(repr(status) for status in ACTION_ITEM_REVIEW_STATUSES)})",
            name="ck_meeting_action_items_review_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_meeting_action_items_confidence_range",
        ),
        CheckConstraint(
            "source_segment_index >= 0",
            name="ck_meeting_action_items_source_segment_index",
        ),
        CheckConstraint(
            "source_start_seconds >= 0 "
            "AND source_end_seconds >= source_start_seconds",
            name="ck_meeting_action_items_source_time_range",
        ),
        CheckConstraint(
            "(source_quote_start_seconds IS NULL AND source_quote_end_seconds IS NULL) "
            "OR (source_quote_start_seconds IS NOT NULL "
            "AND source_quote_end_seconds IS NOT NULL "
            "AND source_quote_start_seconds >= 0 "
            "AND source_quote_end_seconds >= source_quote_start_seconds)",
            name="ck_meeting_action_items_source_quote_time_range",
        ),
        UniqueConstraint(
            "extraction_id",
            "source_segment_index",
            "task_description",
            name="uq_meeting_action_items_extraction_segment_task",
        ),
        Index("ix_meeting_action_items_extraction_id", "extraction_id"),
        Index("ix_meeting_action_items_review_status", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    extraction_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_action_item_extractions.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proposed_employee_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    confirmed_employee_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    employee_match_status: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        default=None,
    )
    ownership_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED,
    )
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deadline_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    source_segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_start_seconds: Mapped[float] = mapped_column(nullable=False)
    source_end_seconds: Mapped[float] = mapped_column(nullable=False)
    source_quote_start_seconds: Mapped[float | None] = mapped_column(nullable=True)
    source_quote_end_seconds: Mapped[float | None] = mapped_column(nullable=True)
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    )
    reviewed_by: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        default=None,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    @property
    def is_task_eligible(self) -> bool:
        return self.review_status == ACTION_ITEM_REVIEW_STATUS_CONFIRMED

    extraction: Mapped["MeetingActionItemExtraction"] = relationship(
        back_populates="items",
    )
    proposed_employee: Mapped["Employee | None"] = relationship(
        foreign_keys="[MeetingActionItem.proposed_employee_id]",
        overlaps="confirmed_employee",
    )
    confirmed_employee: Mapped["Employee | None"] = relationship(
        foreign_keys="[MeetingActionItem.confirmed_employee_id]",
        overlaps="proposed_employee",
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in TASK_STATUSES)})",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            f"priority IN ({', '.join(repr(priority) for priority in ACTION_ITEM_PRIORITIES)})",
            name="ck_tasks_priority",
        ),
        CheckConstraint(
            "assignment_email_status IN ('pending', 'sent', 'failed')",
            name="ck_tasks_assignment_email_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        UniqueConstraint(
            "source_action_item_id",
            name="uq_tasks_source_action_item_id",
        ),
        Index("ix_tasks_assignee_due_at", "assignee_id", "due_at"),
        Index("ix_tasks_meeting_id", "meeting_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    assignee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creator_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_action_item_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_action_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_timestamp_seconds: Mapped[float] = mapped_column(nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TASK_STATUS_PENDING,
    )
    assignment_email_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    assignment_email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignment_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    # Incremented on every due-date change so calendar clients replace the event.
    calendar_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class TaskReassignmentRequest(Base):
    """A cross-team reassignment awaiting HR / Operations or Admin approval."""

    __tablename__ = "task_reassignment_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ", ".join(repr(value) for value in TASK_REASSIGNMENT_STATUSES)
            + ")",
            name="ck_task_reassignment_requests_status",
        ),
        Index("ix_task_reassignment_requests_task_status", "task_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_assignee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_assignee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_role: Mapped[str] = mapped_column(String(40), nullable=False)
    requester_team: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TASK_REASSIGNMENT_STATUS_PENDING,
    )
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TaskAuditLog(Base):
    """Every reassignment / due-date attempt on a task, including denials."""

    __tablename__ = "task_audit_logs"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ("
            + ", ".join(repr(value) for value in TASK_AUDIT_OUTCOMES)
            + ")",
            name="ck_task_audit_logs_outcome",
        ),
        Index("ix_task_audit_logs_task_created_at", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TaskNotification(Base):
    """An e-mail notification about a task change and its delivery outcome."""

    __tablename__ = "task_notifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name="ck_task_notifications_status",
        ),
        Index("ix_task_notifications_task_created_at", "task_id", "created_at"),
        Index(
            "uq_task_notifications_idempotency_key",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_employee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingTranscriptCorrection(Base):
    __tablename__ = "meeting_transcript_corrections"
    __table_args__ = (
        Index(
            "ix_meeting_transcript_corrections_transcript_created_at",
            "transcript_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transcript_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_speaker: Mapped[str] = mapped_column(String(255), nullable=False)
    corrected_speaker: Mapped[str] = mapped_column(String(255), nullable=False)
    editor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingMinutes(Base):
    __tablename__ = "meeting_minutes"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in MINUTES_STATUSES)})",
            name="ck_meeting_minutes_status",
        ),
        CheckConstraint(
            f"policy IN ({', '.join(repr(policy) for policy in MINUTES_POLICIES)})",
            name="ck_meeting_minutes_policy",
        ),
        Index("ix_meeting_minutes_meeting_id", "meeting_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meeting_summaries.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=MINUTES_STATUS_PENDING_APPROVAL,
    )
    policy: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=MINUTES_POLICY_MANUAL_APPROVAL,
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        default=None,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    content_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_decisions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    content_action_items: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="minutes")
    summary: Mapped["MeetingSummary | None"] = relationship()
    recipients: Mapped[list["MeetingMinutesRecipient"]] = relationship(
        back_populates="minutes",
        cascade="all, delete-orphan",
    )


class MeetingMinutesRecipient(Base):
    __tablename__ = "meeting_minutes_recipients"
    __table_args__ = (
        CheckConstraint(
            f"delivery_status IN ({', '.join(repr(status) for status in MINUTES_DELIVERY_STATUSES)})",
            name="ck_meeting_minutes_recipients_delivery_status",
        ),
        UniqueConstraint(
            "minutes_id",
            "recipient_email",
            name="uq_meeting_minutes_recipients_minutes_email",
        ),
        Index(
            "uq_meeting_minutes_recipients_idempotency_key",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_meeting_minutes_recipients_minutes_id", "minutes_id"),
        Index("ix_meeting_minutes_recipients_delivery_status", "delivery_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    minutes_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_minutes.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    attendance_log_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meeting_attendance_logs.id", ondelete="SET NULL"),
        nullable=True,
    )
    delivery_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MINUTES_DELIVERY_STATUS_PENDING,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    minutes: Mapped["MeetingMinutes"] = relationship(back_populates="recipients")
    attendance_log: Mapped["MeetingAttendanceLog | None"] = relationship()
