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
    ACTION_ITEM_EXTRACTION_STATUSES,
    ACTION_ITEM_PRIORITIES,
    Base,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _create_transcript(db_session: Session) -> tuple[Meeting, MeetingTranscript]:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="action-item-organizer",
        title="Action Item Model Test",
        start_at=now,
        end_at=now + timedelta(hours=1),
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
        transcript_text="I will send the draft by Friday.",
        segments=[
            {
                "start": 4.25,
                "end": 7.5,
                "text": "I will send the draft by Friday.",
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.flush()
    return meeting, transcript


def test_create_tables_includes_action_item_tables_and_foreign_keys(
    db_session: Session,
):
    inspector = inspect(db_session.get_bind())
    assert {
        "meeting_action_item_extractions",
        "meeting_action_items",
    }.issubset(set(inspector.get_table_names()))

    extraction_fks = {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in inspector.get_foreign_keys("meeting_action_item_extractions")
    }
    assert extraction_fks == {
        ("meeting_id", "meetings"),
        ("transcript_id", "meeting_transcripts"),
    }
    item_fks = inspector.get_foreign_keys("meeting_action_items")
    assert [
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in item_fks
    ] == [
        ("extraction_id", "meeting_action_item_extractions"),
        ("proposed_employee_id", "employees"),
        ("confirmed_employee_id", "employees"),
    ]


def test_action_item_relationships_preserve_source_and_allow_missing_owner_deadline(
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
    )
    item = MeetingActionItem(
        id=str(uuid.uuid4()),
        owner_name=None,
        task_description="Send the draft.",
        deadline_at=None,
        deadline_text="by Friday",
        priority="medium",
        confidence=0.9,
        source_segment_index=0,
        source_start_seconds=4.25,
        source_end_seconds=7.5,
        source_quote="I will send the draft by Friday.",
    )
    extraction.items.append(item)
    db_session.add(extraction)
    db_session.commit()
    db_session.expire_all()

    saved_extraction = db_session.get(MeetingActionItemExtraction, extraction.id)
    assert saved_extraction is not None
    assert saved_extraction.status == "pending"
    assert saved_extraction.meeting.id == meeting.id
    assert saved_extraction.transcript.id == transcript.id
    assert transcript.action_item_extraction.id == extraction.id
    assert saved_extraction.items[0].owner_name is None
    assert saved_extraction.items[0].deadline_at is None
    assert saved_extraction.items[0].deadline_text == "by Friday"
    assert saved_extraction.items[0].source_start_seconds == 4.25
    assert saved_extraction.items[0].source_end_seconds == 7.5


def test_action_item_extraction_and_item_constraints(db_session: Session):
    meeting, transcript = _create_transcript(db_session)
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
        status=ACTION_ITEM_EXTRACTION_STATUSES[-1],
    )
    db_session.add(extraction)
    db_session.commit()

    invalid_status = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
        status="unknown",
    )
    db_session.add(invalid_status)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    invalid_items = [
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority="unknown",
            confidence=0.5,
            source_segment_index=0,
            source_start_seconds=0,
            source_end_seconds=1,
        ),
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority=ACTION_ITEM_PRIORITIES[0],
            confidence=1.1,
            source_segment_index=0,
            source_start_seconds=0,
            source_end_seconds=1,
        ),
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority=ACTION_ITEM_PRIORITIES[0],
            confidence=0.5,
            source_segment_index=-1,
            source_start_seconds=0,
            source_end_seconds=1,
        ),
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority=ACTION_ITEM_PRIORITIES[0],
            confidence=0.5,
            source_segment_index=0,
            source_start_seconds=2,
            source_end_seconds=1,
        ),
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority=ACTION_ITEM_PRIORITIES[0],
            confidence=0.5,
            source_segment_index=0,
            source_start_seconds=-1,
            source_end_seconds=1,
        ),
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority=ACTION_ITEM_PRIORITIES[0],
            confidence=0.5,
            source_segment_index=0,
            source_start_seconds=0,
            source_end_seconds=1,
            source_quote_start_seconds=0.5,
        ),
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction.id,
            task_description="Task",
            priority=ACTION_ITEM_PRIORITIES[0],
            confidence=0.5,
            source_segment_index=0,
            source_start_seconds=0,
            source_end_seconds=1,
            source_quote_start_seconds=0.8,
            source_quote_end_seconds=0.5,
        ),
    ]
    db_session.add_all(invalid_items)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_unique_constraints_prevent_duplicate_extractions_and_items(
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    extraction_id = str(uuid.uuid4())
    extraction = MeetingActionItemExtraction(
        id=extraction_id,
        meeting_id=meeting.id,
        transcript_id=transcript.id,
    )
    db_session.add(extraction)
    db_session.commit()

    db_session.add(
        MeetingActionItemExtraction(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=transcript.id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction_id,
            task_description="Send the draft.",
            priority="medium",
            confidence=0.8,
            source_segment_index=0,
            source_start_seconds=4,
            source_end_seconds=7,
        )
    )
    db_session.commit()
    db_session.add(
        MeetingActionItem(
            id=str(uuid.uuid4()),
            extraction_id=extraction_id,
            task_description="Send the draft.",
            priority="medium",
            confidence=0.9,
            source_segment_index=0,
            source_start_seconds=4,
            source_end_seconds=7,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_alembic_upgrade_head_creates_action_item_schema(tmp_path: Path):
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "migration-test.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    baseline_engine = create_engine(database_url)
    try:
        baseline_metadata = sa.MetaData()
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
        baseline_metadata.create_all(baseline_engine)
    finally:
        baseline_engine.dispose()

    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url,
    )

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "meeting_action_item_extractions" in inspector.get_table_names()
        assert "meeting_action_items" in inspector.get_table_names()
        assert {
            "participant_name",
            "connection_id",
        }.issubset(
            {
                column["name"]
                for column in inspector.get_columns("meeting_recordings")
            }
        )
        assert any(
            unique["name"] == "uq_meeting_action_item_extractions_transcript_id"
            for unique in inspector.get_unique_constraints(
                "meeting_action_item_extractions"
            )
        )
        assert {
            "owner_name",
            "task_description",
            "deadline_at",
            "deadline_text",
            "priority",
            "confidence",
            "source_segment_index",
            "source_start_seconds",
            "source_end_seconds",
            "source_quote_start_seconds",
            "source_quote_end_seconds",
            "source_quote",
        }.issubset(
            {
                column["name"]
                for column in inspector.get_columns("meeting_action_items")
            }
        )
    finally:
        engine.dispose()
