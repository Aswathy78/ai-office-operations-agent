"""Opt-in large-v3 p90 check.

Set STT_PERFORMANCE_AUDIO_PATHS to at least ten 60-minute English audio files,
separated by the platform path separator, and run this test on the target worker.
"""

from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.meeting.models import (
    Base,
    Meeting,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_PENDING,
)
from app.meeting.recording_encryption import encrypt_recording_file
from app.meeting.transcription_worker import (
    claim_pending_transcript,
    create_whisper_model,
    process_claimed_transcript,
)

PERFORMANCE_AUDIO_PATHS = os.environ.get("STT_PERFORMANCE_AUDIO_PATHS")


@pytest.mark.skipif(not PERFORMANCE_AUDIO_PATHS, reason="No 60-minute STT benchmark corpus configured")
def test_large_v3_transcription_p90_is_within_ten_minutes(tmp_path):
    import av

    audio_paths = [Path(path) for path in PERFORMANCE_AUDIO_PATHS.split(os.pathsep)]
    if len(audio_paths) < 10:
        pytest.fail("STT_PERFORMANCE_AUDIO_PATHS must contain at least ten audio files.")
    if any(not path.is_file() for path in audio_paths):
        pytest.fail("Every STT performance audio path must point to an existing file.")
    for audio_path in audio_paths:
        with av.open(str(audio_path)) as container:
            if not container.streams.audio or container.duration is None:
                pytest.fail(f"{audio_path} must contain a measurable audio stream.")
            audio_stream = container.streams.audio[0]
            duration_seconds = audio_stream.duration * audio_stream.time_base
            if not 3540 <= duration_seconds <= 3660:
                pytest.fail(f"{audio_path} is not a 60-minute audio sample.")

    encryption_key = os.environ.get("RECORDING_ENCRYPTION_KEY")
    if not encryption_key:
        pytest.fail("RECORDING_ENCRYPTION_KEY must be set for the STT performance test.")

    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    durations: list[float] = []

    with Session(engine) as db:
        now = datetime.now(timezone.utc)
        meeting_id = str(uuid.uuid4())
        db.add(
            Meeting(
                id=meeting_id,
                organizer_id="stt-performance-test",
                title="STT performance benchmark",
                start_at=now,
                end_at=now + timedelta(hours=1),
                join_url=f"http://localhost/meetings/{meeting_id}",
                status="completed",
            )
        )
        recordings = []
        for audio_path in audio_paths:
            recording_id = str(uuid.uuid4())
            relative_directory = storage_root / meeting_id
            relative_directory.mkdir(parents=True, exist_ok=True)
            encrypted_path = relative_directory / f"{recording_id}{audio_path.suffix}"
            encrypt_recording_file(audio_path, encrypted_path)
            recording = MeetingRecording(
                id=recording_id,
                meeting_id=meeting_id,
                status=RECORDING_STATUS_COMPLETED,
                storage_uri=f"{meeting_id}/{recording_id}{audio_path.suffix}",
                completed_at=now,
            )
            recordings.append(recording)
        db.add_all(recordings)
        db.commit()

        model = create_whisper_model()
        for recording in recordings:
            db.add(
                MeetingTranscript(
                    id=str(uuid.uuid4()),
                    recording_id=recording.id,
                    status=TRANSCRIPT_STATUS_PENDING,
                    language="en",
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            started_at = time.perf_counter()
            transcript_id = claim_pending_transcript(db)
            assert transcript_id is not None
            processed = process_claimed_transcript(
                db,
                transcript_id,
                model,
                storage_root=storage_root,
                temporary_directory=tmp_path,
            )
            durations.append(time.perf_counter() - started_at)
            assert processed

    engine.dispose()
    durations.sort()
    p90 = durations[math.ceil(0.9 * len(durations)) - 1]
    assert p90 <= 600, f"large-v3 transcription p90 was {p90:.1f}s, expected <= 600s"