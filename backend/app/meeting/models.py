from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Allowed values for MeetingGuestRequest.status
GUEST_STATUS_PENDING = "pending"
GUEST_STATUS_ADMITTED = "admitted"
GUEST_STATUS_REJECTED = "rejected"


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
