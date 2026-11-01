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
TASK_STATUS_COMPLETED = "completed"
TASK_STATUSES = (TASK_STATUS_PENDING, TASK_STATUS_COMPLETED)

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

RESCHEDULE_STATUS_PENDING = "pending"
RESCHEDULE_STATUS_APPROVED = "approved"
RESCHEDULE_STATUS_REJECTED = "rejected"
RESCHEDULE_STATUSES = (
    RESCHEDULE_STATUS_PENDING,
    RESCHEDULE_STATUS_APPROVED,
    RESCHEDULE_STATUS_REJECTED,
)

CANCELLATION_STATUS_PENDING = "pending"
CANCELLATION_STATUS_APPROVED = "approved"
CANCELLATION_STATUS_REJECTED = "rejected"
CANCELLATION_STATUSES = (
    CANCELLATION_STATUS_PENDING,
    CANCELLATION_STATUS_APPROVED,
    CANCELLATION_STATUS_REJECTED,
)

AUTH_ACCOUNT_ROLES = ("employee", "manager", "hr_operations", "admin")


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

    reschedule_requests: Mapped[list["MeetingRescheduleRequest"]] = relationship(
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
    # Line manager; receives escalations when this employee's tasks go unanswered.
    manager_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    timezone_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    working_days: Mapped[list[int]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: [0, 1, 2, 3, 4],
    )
    working_hours_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default="09:00"
    )
    working_hours_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default="17:00"
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
    # Meeting provenance is set for "meeting_action_item" tasks and empty for tasks
    # the AI assistant creates directly (source "assistant", Story 6.5).
    source_action_item_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meeting_action_items.id", ondelete="CASCADE"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    meeting_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=True,
    )
    transcript_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_segment_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_timestamp_seconds: Mapped[float | None] = mapped_column(nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TASK_STATUS_PENDING,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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


USER_NOTIFICATION_EMAIL_STATUSES = (
    "disabled",
    "pending",
    "deferred",
    "sending",
    "retrying",
    "sent",
    "failed",
)


class UserNotification(Base):
    """Recipient-scoped inbox item and its independent channel delivery state."""

    __tablename__ = "user_notifications"
    __table_args__ = (
        CheckConstraint(
            "in_app_status IN ('delivered', 'disabled')",
            name="ck_user_notifications_in_app_status",
        ),
        CheckConstraint(
            f"email_status IN ({', '.join(repr(status) for status in USER_NOTIFICATION_EMAIL_STATUSES)})",
            name="ck_user_notifications_email_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_user_notifications_idempotency_key"),
        UniqueConstraint("task_notification_id", name="uq_user_notifications_task_notification"),
        Index(
            "ix_user_notifications_recipient_created",
            "recipient_employee_id",
            "created_at",
        ),
        Index(
            "ix_user_notifications_email_due",
            "email_status",
            "email_next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recipient_employee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entity_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    in_app_status: Mapped[str] = mapped_column(String(20), nullable=False, default="delivered")
    email_status: Mapped[str] = mapped_column(String(20), nullable=False, default="disabled")
    email_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    email_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_notification_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("task_notifications.id", ondelete="SET NULL"),
        nullable=True,
    )
    email_failure_notification_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("email_failure_notifications.id", ondelete="SET NULL"),
        nullable=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class NotificationPreference(Base):
    """Per-employee channel preferences and local quiet hours."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        CheckConstraint(
            "(quiet_hours_start IS NULL AND quiet_hours_end IS NULL) OR "
            "(quiet_hours_start IS NOT NULL AND quiet_hours_end IS NOT NULL)",
            name="ck_notification_preferences_quiet_hours_pair",
        ),
    )

    employee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
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


# EmailDraft.status — the delivery lifecycle (Story 4.4 / FR-22). Every e-mail
# has exactly one of these seven statuses; allowed moves are defined in
# app.email_delivery_service.ALLOWED_TRANSITIONS.
EMAIL_DRAFT_STATUS_DRAFT = "draft"
EMAIL_DRAFT_STATUS_APPROVAL = "approval"
EMAIL_DRAFT_STATUS_QUEUED = "queued"
EMAIL_DRAFT_STATUS_SENDING = "sending"
EMAIL_DRAFT_STATUS_SENT = "sent"
EMAIL_DRAFT_STATUS_FAILED = "failed"
EMAIL_DRAFT_STATUS_RETRY = "retry"
EMAIL_DRAFT_STATUSES = (
    EMAIL_DRAFT_STATUS_DRAFT,
    EMAIL_DRAFT_STATUS_APPROVAL,
    EMAIL_DRAFT_STATUS_QUEUED,
    EMAIL_DRAFT_STATUS_SENDING,
    EMAIL_DRAFT_STATUS_SENT,
    EMAIL_DRAFT_STATUS_FAILED,
    EMAIL_DRAFT_STATUS_RETRY,
)

# Outcome of an approval request; the draft returns to "draft" either way.
EMAIL_APPROVAL_DECISION_APPROVED = "approved"
EMAIL_APPROVAL_DECISION_REJECTED = "rejected"

EMAIL_DRAFT_SOURCE_TEMPLATE = "template"
EMAIL_DRAFT_SOURCE_LLM = "llm"
EMAIL_DRAFT_SOURCES = (EMAIL_DRAFT_SOURCE_TEMPLATE, EMAIL_DRAFT_SOURCE_LLM)


class EmailDraft(Base):
    """An e-mail prepared by the assistant that the employee previews, edits and sends."""

    __tablename__ = "email_drafts"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in EMAIL_DRAFT_STATUSES)})",
            name="ck_email_drafts_status",
        ),
        CheckConstraint(
            f"source IN ({', '.join(repr(source) for source in EMAIL_DRAFT_SOURCES)})",
            name="ck_email_drafts_source",
        ),
        Index("ix_email_drafts_created_by_id", "created_by_id"),
        Index("ix_email_drafts_status", "status"),
        Index("ix_email_drafts_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_role: Mapped[str] = mapped_column(String(30), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    generation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # [{"name", "email", "internal", "source"}]
    recipients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # [{"name", "count"}] — directory groups that were expanded into recipients.
    recipient_groups: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    unresolved_recipients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Story 7.4: company documents whose facts the draft quotes; the sender's access
    # to them is re-checked when the e-mail is sent.
    source_document_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # [{"type", "label", "severity", "match", "field"}]
    sensitive_findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=EMAIL_DRAFT_STATUS_DRAFT,
    )
    # "approved" / "rejected" once an approver has decided; cleared by any edit.
    approval_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # One entry per recipient: {"email", "status": "sent" | "failed", "error",
    # "message_id", "accepted_at", "attempt"}. "sent" means the provider accepted it.
    delivery_results: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Delivery bookkeeping.
    acknowledged_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Story 4.6: [{"recipient", "type": "hard" | "soft", "source": "smtp" | "dsn" | "ses",
    #              "code", "diagnostic", "message_id", "recorded_at"}]
    bounces: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # When the requester and admins were last told the e-mail failed.
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_notifications: Mapped[list["EmailFailureNotification"]] = relationship(
        order_by="EmailFailureNotification.created_at",
        lazy="selectin",
        viewonly=True,
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
    )


class EmailDraftEvent(Base):
    """One status change of an e-mail draft, so the sender can see exactly what happened."""

    __tablename__ = "email_draft_events"
    __table_args__ = (
        Index("ix_email_draft_events_draft_created", "draft_id", "created_at"),
        UniqueConstraint("draft_id", "sequence", name="uq_email_draft_events_draft_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("email_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 1, 2, 3, ... per e-mail; orders events even when timestamps tie.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Who caused it: a user id, or "system" for the dispatcher.
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingRescheduleRequest(Base):
    """A meeting reschedule request awaiting organizer approval (Story 5.3)."""

    __tablename__ = "meeting_reschedule_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ", ".join(repr(value) for value in RESCHEDULE_STATUSES)
            + ")",
            name="ck_meeting_reschedule_requests_status",
        ),
        Index("ix_meeting_reschedule_requests_meeting_status", "meeting_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    new_end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=RESCHEDULE_STATUS_PENDING,
    )
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    meeting: Mapped["Meeting"] = relationship(
        "Meeting",
        back_populates="reschedule_requests",
    )


class MeetingDelegate(Base):
    """Explicit authorized delegates for meeting scheduling/rescheduling (Story 5.3)."""

    __tablename__ = "meeting_delegates"
    __table_args__ = (
        UniqueConstraint(
            "organizer_id",
            "delegate_id",
            "meeting_id",
            name="uq_meeting_delegates_organizer_delegate_meeting",
        ),
        Index("ix_meeting_delegates_organizer_delegate", "organizer_id", "delegate_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organizer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    delegate_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    meeting_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AuthAccount(Base):
    """Operator-provisioned login account for one existing employee."""

    __tablename__ = "auth_accounts"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_auth_accounts_employee_id"),
        UniqueConstraint("email_normalized", name="uq_auth_accounts_email_normalized"),
        CheckConstraint(
            "role IN (" + ", ".join(repr(role) for role in AUTH_ACCOUNT_ROLES) + ")",
            name="ck_auth_accounts_role",
        ),
        Index("ix_auth_accounts_enabled_role", "is_enabled", "role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    employee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    email_normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="employee")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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


class AuthSession(Base):
    """A signed access token's server-side revocation and expiry record."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_account_expires", "account_id", "expires_at"),
    )

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("auth_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingCancellationRequest(Base):
    """A cancellation awaiting organizer/delegate approval (Story 5.4)."""

    __tablename__ = "meeting_cancellation_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ", ".join(repr(value) for value in CANCELLATION_STATUSES)
            + ")",
            name="ck_meeting_cancellation_requests_status",
        ),
        Index("ix_meeting_cancellation_requests_meeting_status", "meeting_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(36), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=CANCELLATION_STATUS_PENDING,
    )
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MeetingAuditLog(Base):
    """Persistent audit history for meeting cancellations and participant changes."""

    __tablename__ = "meeting_audit_logs"
    __table_args__ = (
        Index("ix_meeting_audit_logs_meeting_created_at", "meeting_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Intentionally not a foreign key: permanent meeting deletion must not erase audit history.
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


EMAIL_NOTIFICATION_STATUSES = ("pending", "sent", "failed", "recorded")


class EmailFailureNotification(Base):
    """Someone told that an e-mail failed (Story 4.6 / FR-23).

    One row per audience member per failure. ``channel`` "email" rows are sent
    through the mail providers and retried if that fails; the "in_app" admin row
    is always written, so a failure is never lost even when mail is down.
    """

    __tablename__ = "email_failure_notifications"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in EMAIL_NOTIFICATION_STATUSES)})",
            name="ck_email_failure_notifications_status",
        ),
        CheckConstraint("audience IN ('requester', 'admin')", name="ck_email_failure_notifications_audience"),
        CheckConstraint("channel IN ('email', 'in_app')", name="ck_email_failure_notifications_channel"),
        UniqueConstraint("idempotency_key", name="uq_email_failure_notifications_idempotency_key"),
        Index("ix_email_failure_notifications_draft", "draft_id", "created_at"),
        Index("ix_email_failure_notifications_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("email_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Sequence of the EmailDraftEvent that moved the e-mail to Failed.
    failure_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# Task deadline calendar sync (Story 5.5 / FR-28): exactly one status per task.
CALENDAR_SYNC_STATUS_PENDING = "pending"
CALENDAR_SYNC_STATUS_PROCESSING = "processing"
CALENDAR_SYNC_STATUS_COMPLETED = "completed"
CALENDAR_SYNC_STATUS_RETRYING = "retrying"
CALENDAR_SYNC_STATUS_FAILED = "failed"
CALENDAR_SYNC_STATUSES = (
    CALENDAR_SYNC_STATUS_PENDING,
    CALENDAR_SYNC_STATUS_PROCESSING,
    CALENDAR_SYNC_STATUS_COMPLETED,
    CALENDAR_SYNC_STATUS_RETRYING,
    CALENDAR_SYNC_STATUS_FAILED,
)


class CalendarDeadlineEntry(Base):
    """A deadline entry held by the *local* calendar provider (Story 5.5).

    This table plays the role of the external calendar for local development:
    the app writes entries through ``LocalCalendarProvider`` and the poller reads
    them back. Anything that edits a row here outside the sync service (bumping
    ``version``) is an "external change", as a user editing Google/Outlook would be.
    """

    __tablename__ = "calendar_deadline_entries"
    __table_args__ = (
        Index("ix_calendar_deadline_entries_owner", "owner_email"),
        Index("ix_calendar_deadline_entries_external_ref", "external_ref"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reminder_minutes_before: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    # The app object this entry mirrors, e.g. "task:<id>".
    external_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Incremented on every change; the poller compares it with what was last synced.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TaskCalendarSync(Base):
    """Calendar sync state of one task's deadline (Story 5.5 / FR-28).

    ``status`` is the single status shown to users. ``completed`` is only set after
    the provider's copy was read back and matched the task's assignee, title and due date.
    """

    __tablename__ = "task_calendar_syncs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in CALENDAR_SYNC_STATUSES)})",
            name="ck_task_calendar_syncs_status",
        ),
        UniqueConstraint("task_id", name="uq_task_calendar_syncs_task_id"),
        Index("ix_task_calendar_syncs_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="local")
    external_entry_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CALENDAR_SYNC_STATUS_PENDING)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why the entry needs (re)syncing, e.g. "Due date changed" or "Moved in the calendar".
    pending_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshot of what the provider holds after the last completed sync.
    synced_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_assignee_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    synced_owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synced_entry_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_poll_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_change_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_change_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AssistantConversation(Base):
    """Conversation session for the AI Assistant command center (Epic 6.1 / FR-30)."""

    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pending_clarification: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Story 6.4: JSON execution checkpoint (plan, completed steps, token budget).
    # Never overloaded with clarification state — kept in a separate column.
    execution_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    messages: Mapped[list["AssistantMessage"]] = relationship(
        "AssistantMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.created_at",
    )


class AssistantMessage(Base):
    """Individual message in an assistant conversation (Epic 6.1 / FR-30)."""

    __tablename__ = "assistant_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # 'user', 'assistant', 'system'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    conversation: Mapped["AssistantConversation"] = relationship(
        "AssistantConversation",
        back_populates="messages",
    )


# Story 6.5: one row per assistant tool call, also the idempotency ledger for writes.
TOOL_EXECUTION_STATUS_RUNNING = "running"
TOOL_EXECUTION_STATUS_SUCCEEDED = "succeeded"
TOOL_EXECUTION_STATUS_FAILED = "failed"
TOOL_EXECUTION_STATUS_TIMED_OUT = "timed_out"
TOOL_EXECUTION_STATUSES = (
    TOOL_EXECUTION_STATUS_RUNNING,
    TOOL_EXECUTION_STATUS_SUCCEEDED,
    TOOL_EXECUTION_STATUS_FAILED,
    TOOL_EXECUTION_STATUS_TIMED_OUT,
)


class AssistantToolExecution(Base):
    """An AI assistant tool call and its outcome (Story 6.5).

    Write tools must carry an idempotency key; ``(actor_id, idempotency_key)`` is
    unique, so repeating a call returns the stored result instead of acting twice.
    """

    __tablename__ = "assistant_tool_executions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in TOOL_EXECUTION_STATUSES)})",
            name="ck_assistant_tool_executions_status",
        ),
        UniqueConstraint(
            "actor_id",
            "idempotency_key",
            name="uq_assistant_tool_executions_actor_key",
        ),
        Index("ix_assistant_tool_executions_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(30), nullable=False)
    # Intentionally not a foreign key: deleting a conversation keeps the audit trail.
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    risk_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TOOL_EXECUTION_STATUS_RUNNING,
    )
    # Partial progress a handler saved (e.g. the id of a record it already
    # created) so a re-run after a failure or timeout resumes instead of duplicating.
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Story 6.6: approvals for assistant writes that are not low risk.
ACTION_APPROVAL_STATUS_PENDING = "pending"
ACTION_APPROVAL_STATUS_REJECTED = "rejected"
ACTION_APPROVAL_STATUS_CANCELLED = "cancelled"
ACTION_APPROVAL_STATUS_EXPIRED = "expired"
ACTION_APPROVAL_STATUS_EXECUTED = "executed"
ACTION_APPROVAL_STATUS_FAILED = "failed"
ACTION_APPROVAL_STATUSES = (
    ACTION_APPROVAL_STATUS_PENDING,
    ACTION_APPROVAL_STATUS_REJECTED,
    ACTION_APPROVAL_STATUS_CANCELLED,
    ACTION_APPROVAL_STATUS_EXPIRED,
    ACTION_APPROVAL_STATUS_EXECUTED,
    ACTION_APPROVAL_STATUS_FAILED,
)
RISK_LEVELS = ("low", "medium", "high")


class AssistantActionApproval(Base):
    """An assistant write paused with "Approval Required" (Story 6.6).

    ``approver_kind`` is "requester" for medium risk (the requester confirms) and
    "approver" for high risk (someone else with approval rights decides). On
    approval the action runs with the requester's own permissions.
    """

    __tablename__ = "assistant_action_approvals"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in ACTION_APPROVAL_STATUSES)})",
            name="ck_assistant_action_approvals_status",
        ),
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')",
            name="ck_assistant_action_approvals_risk_level",
        ),
        CheckConstraint(
            "approver_kind IN ('requester', 'approver')",
            name="ck_assistant_action_approvals_approver_kind",
        ),
        UniqueConstraint(
            "requester_id",
            "idempotency_key",
            name="uq_assistant_action_approvals_requester_key",
        ),
        Index("ix_assistant_action_approvals_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_role: Mapped[str] = mapped_column(String(30), nullable=False)
    requester_team: Mapped[str | None] = mapped_column(String(100), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)
    risk_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    approver_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ACTION_APPROVAL_STATUS_PENDING
    )
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_by_role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AssistantAuditEvent(Base):
    """One permission, conflict, risk or approval decision about an assistant tool call (Story 6.6)."""

    __tablename__ = "assistant_audit_events"
    __table_args__ = (
        Index("ix_assistant_audit_events_actor_created", "actor_id", "created_at"),
        Index("ix_assistant_audit_events_approval", "approval_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Who caused the event: the requester, or the approver for approval decisions.
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(30), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    # permission_denied, conflict_blocked, invalid_arguments, auto_approved,
    # approval_requested, approved, rejected, cancelled, expired, executed,
    # execution_failed, replayed, verification_succeeded, verification_escalated
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
