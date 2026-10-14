from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.models import (
    Base,
    Meeting,
    MeetingAttendanceLog,
    MeetingMinutes,
    MeetingMinutesRecipient,
    MeetingSummary,
    MeetingTranscript,
    MeetingRecording,
    MINUTES_DELIVERY_STATUS_FAILED,
    MINUTES_DELIVERY_STATUS_PENDING,
    MINUTES_DELIVERY_STATUS_SENT,
    MINUTES_POLICY_AUTO_SEND,
    MINUTES_POLICY_MANUAL_APPROVAL,
    MINUTES_STATUS_APPROVED,
    MINUTES_STATUS_DRAFT,
    MINUTES_STATUS_FAILED,
    MINUTES_STATUS_PENDING_APPROVAL,
    MINUTES_STATUS_SENT,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _create_meeting_with_attendance(
    db_session: Session,
) -> tuple[Meeting, MeetingSummary, MeetingAttendanceLog]:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="test-organizer-id",
        title="Weekly Product Sync",
        start_at=now - timedelta(hours=1),
        end_at=now,
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
    )
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_COMPLETED,
        overview="Product sync discussion overview.",
        decisions=["Ship MVP by Friday."],
    )
    attendance = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Sarah Connor",
        participant_email="sarah@example.com",
        user_id="user-sarah",
        is_external_guest=False,
    )
    db_session.add_all([meeting, recording, transcript, summary, attendance])
    db_session.flush()
    return meeting, summary, attendance


def test_create_tables_includes_meeting_minutes_and_recipients(db_session: Session):
    inspector = inspect(db_session.get_bind())
    tables = set(inspector.get_table_names())
    assert "meeting_minutes" in tables
    assert "meeting_minutes_recipients" in tables

    minutes_fks = {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in inspector.get_foreign_keys("meeting_minutes")
    }
    assert ("meeting_id", "meetings") in minutes_fks
    assert ("summary_id", "meeting_summaries") in minutes_fks

    recipient_fks = {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in inspector.get_foreign_keys("meeting_minutes_recipients")
    }
    assert ("minutes_id", "meeting_minutes") in recipient_fks
    assert ("attendance_log_id", "meeting_attendance_logs") in recipient_fks


def test_meeting_minutes_defaults_to_manual_approval_and_pending_status(db_session: Session):
    meeting, summary, _ = _create_meeting_with_attendance(db_session)
    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
    )
    db_session.add(minutes)
    db_session.commit()
    db_session.refresh(minutes)

    assert minutes.policy == MINUTES_POLICY_MANUAL_APPROVAL
    assert minutes.status == MINUTES_STATUS_PENDING_APPROVAL
    assert minutes.approved_by is None
    assert minutes.approved_at is None
    assert minutes.created_at is not None
    assert minutes.updated_at is not None


def test_recipient_delivery_record_defaults_to_pending(db_session: Session):
    meeting, summary, attendance = _create_meeting_with_attendance(db_session)
    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
    )
    db_session.add(minutes)
    db_session.flush()

    recipient = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Sarah Connor",
        recipient_email="sarah@example.com",
        attendance_log_id=attendance.id,
    )
    db_session.add(recipient)
    db_session.commit()
    db_session.refresh(recipient)

    assert recipient.delivery_status == MINUTES_DELIVERY_STATUS_PENDING
    assert recipient.sent_at is None
    assert recipient.error_message is None
    assert recipient.attendance_log_id == attendance.id
    assert recipient.attendance_log.participant_name == "Sarah Connor"


def test_duplicate_recipient_for_same_minutes_violates_unique_constraint(db_session: Session):
    meeting, summary, attendance = _create_meeting_with_attendance(db_session)
    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
    )
    db_session.add(minutes)
    db_session.flush()

    recipient1 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Sarah Connor",
        recipient_email="sarah@example.com",
        attendance_log_id=attendance.id,
    )
    db_session.add(recipient1)
    db_session.commit()

    recipient2 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Sarah Connor Alternate",
        recipient_email="sarah@example.com",
        attendance_log_id=attendance.id,
    )
    db_session.add(recipient2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_duplicate_idempotency_key_violates_unique_constraint(db_session: Session):
    meeting, summary, _ = _create_meeting_with_attendance(db_session)
    minutes1 = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
    )
    minutes2 = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
    )
    db_session.add_all([minutes1, minutes2])
    db_session.flush()

    idempotency_key = "minutes:test-batch:sarah@example.com"
    rec1 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes1.id,
        recipient_name="Sarah Connor",
        recipient_email="sarah@example.com",
        idempotency_key=idempotency_key,
    )
    db_session.add(rec1)
    db_session.commit()

    rec2 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes2.id,
        recipient_name="Sarah Connor",
        recipient_email="other@example.com",
        idempotency_key=idempotency_key,
    )
    db_session.add(rec2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_cascade_delete_meeting_removes_minutes_and_recipients(db_session: Session):
    meeting, summary, attendance = _create_meeting_with_attendance(db_session)
    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
    )
    db_session.add(minutes)
    db_session.flush()

    recipient = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Sarah Connor",
        recipient_email="sarah@example.com",
        attendance_log_id=attendance.id,
    )
    db_session.add(recipient)
    db_session.commit()

    minutes_id = minutes.id
    recipient_id = recipient.id

    db_session.delete(meeting)
    db_session.commit()

    assert db_session.get(MeetingMinutes, minutes_id) is None
    assert db_session.get(MeetingMinutesRecipient, recipient_id) is None


def test_alembic_upgrade_and_downgrade_meeting_minutes(tmp_path: Path):
    backend_dir = Path(__file__).resolve().parents[1]
    db_file = tmp_path / "test_migration.db"
    database_url = f"sqlite:///{db_file.as_posix()}"

    baseline_engine = create_engine(database_url)
    try:
        baseline_metadata = sa.MetaData()
        sa.Table(
            "meetings",
            baseline_metadata,
            sa.Column("id", sa.String(length=36), primary_key=True),
        )
        sa.Table(
            "meeting_recordings",
            baseline_metadata,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("meeting_id", sa.String(length=36), nullable=False),
            sa.Column("participant_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "retention_expires_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("storage_uri", sa.String(length=1000), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
        )
        sa.Table(
            "meeting_transcripts",
            baseline_metadata,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "recording_id",
                sa.String(length=36),
                sa.ForeignKey("meeting_recordings.id"),
                nullable=False,
            ),
        )
        sa.Table(
            "meeting_attendance_logs",
            baseline_metadata,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "meeting_id",
                sa.String(length=36),
                sa.ForeignKey("meetings.id"),
                nullable=False,
            ),
            sa.Column("participant_name", sa.String(length=255), nullable=False),
            sa.Column("participant_email", sa.String(length=255), nullable=True),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("is_external_guest", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("joined_at", sa.DateTime(), nullable=False),
            sa.Column("left_at", sa.DateTime(), nullable=True),
        )
        baseline_metadata.create_all(baseline_engine)
    finally:
        baseline_engine.dispose()

    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)

    # Upgrade to head (including 20261014_03)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "meeting_minutes" in table_names
        assert "meeting_minutes_recipients" in table_names

        minutes_cols = {col["name"] for col in inspector.get_columns("meeting_minutes")}
        assert {
            "id",
            "meeting_id",
            "summary_id",
            "status",
            "policy",
            "approved_by",
            "approved_at",
            "content_summary",
            "content_decisions",
            "content_action_items",
            "error_message",
            "created_at",
            "updated_at",
        }.issubset(minutes_cols)

        recipient_cols = {
            col["name"] for col in inspector.get_columns("meeting_minutes_recipients")
        }
        assert {
            "id",
            "minutes_id",
            "recipient_name",
            "recipient_email",
            "attendance_log_id",
            "delivery_status",
            "error_message",
            "idempotency_key",
            "sent_at",
            "created_at",
        }.issubset(recipient_cols)

        assert any(
            index["name"] == "uq_meeting_minutes_recipients_idempotency_key"
            and index["unique"]
            for index in inspector.get_indexes("meeting_minutes_recipients")
        )
    finally:
        engine.dispose()

    # Downgrade to previous revision (20261014_02)
    command.downgrade(config, "20261014_02")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "meeting_minutes" not in table_names
        assert "meeting_minutes_recipients" not in table_names
    finally:
        engine.dispose()
