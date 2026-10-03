from datetime import datetime, timezone
from datetime import timedelta
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
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
