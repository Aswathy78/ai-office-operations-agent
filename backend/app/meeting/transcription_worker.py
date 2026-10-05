"""Database-backed Whisper worker for completed meeting recordings.

Run from the backend directory with:

    python -m app.meeting.transcription_worker
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Callable

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.meeting.models import (
    MeetingRecording,
    MeetingTranscript,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
    TRANSCRIPT_STATUS_PENDING,
    TRANSCRIPT_STATUS_PROCESSING,
)
from app.meeting.service import RECORDING_STORAGE_DIR
from app.meeting.speaker_identification import identify_recording_speaker
from app.meeting.transcription_media import decrypt_recording_to_temporary_file

logger = logging.getLogger(__name__)


def create_whisper_model():
    from faster_whisper import WhisperModel

    return WhisperModel(
        "large-v3",
        device=os.environ.get("WHISPER_DEVICE", "cpu"),
        compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
    )


def claim_pending_transcript(db: Session) -> str | None:
    transcript_id = (
        db.query(MeetingTranscript.id)
        .filter(MeetingTranscript.status == TRANSCRIPT_STATUS_PENDING)
        .order_by(MeetingTranscript.created_at, MeetingTranscript.id)
        .limit(1)
        .scalar()
    )

    if transcript_id is None:
        return None

    result = (
        db.query(MeetingTranscript)
        .filter(
            MeetingTranscript.id == transcript_id,
            MeetingTranscript.status == TRANSCRIPT_STATUS_PENDING,
        )
        .update(
            {
                MeetingTranscript.status: TRANSCRIPT_STATUS_PROCESSING,
                MeetingTranscript.processing_started_at: datetime.now(timezone.utc),
                MeetingTranscript.error_message: None,
            },
            synchronize_session=False,
        )
    )

    db.commit()
    db.expire_all()

    return transcript_id if result == 1 else None


def _recording_path(
    recording: MeetingRecording,
    storage_root: str | Path,
) -> Path:
    if not recording.storage_uri or "\\" in recording.storage_uri:
        raise ValueError("Recording has no safe storage URI.")

    relative_path = PurePosixPath(recording.storage_uri)

    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 2
        or any(part in ("", ".", "..") for part in relative_path.parts)
        or ":" in recording.storage_uri
    ):
        raise ValueError("Recording storage URI is invalid.")

    meeting_directory, filename = relative_path.parts

    if (
        meeting_directory != recording.meeting_id
        or not filename.startswith(f"{recording.id}.")
    ):
        raise ValueError(
            "Recording storage URI does not match its database record."
        )

    root = Path(storage_root).resolve()
    candidate = root.joinpath(*relative_path.parts)
    resolved_candidate = candidate.resolve(strict=True)

    if (
        not resolved_candidate.is_relative_to(root)
        or not resolved_candidate.is_file()
    ):
        raise ValueError(
            "Recording file is outside the configured storage directory."
        )

    return resolved_candidate


def _persist_failure(
    db: Session,
    transcript_id: str,
    error: Exception,
) -> None:
    db.rollback()

    transcript = db.get(MeetingTranscript, transcript_id)

    if transcript is None:
        return

    if transcript.status != TRANSCRIPT_STATUS_PROCESSING:
        return

    transcript.status = TRANSCRIPT_STATUS_FAILED
    transcript.error_message = (
        f"{type(error).__name__}: {error}"
    )[:4000]
    transcript.completed_at = datetime.now(timezone.utc)

    db.commit()


def process_claimed_transcript(
    db: Session,
    transcript_id: str,
    model,
    *,
    storage_root: str | Path = RECORDING_STORAGE_DIR,
    temporary_directory: str | Path | None = None,
) -> bool:

    plaintext_path: Path | None = None

    try:
        # ---------------------------------------------------------
        # 1. Load transcript
        # ---------------------------------------------------------

        transcript = db.get(MeetingTranscript, transcript_id)

        if transcript is None:
            return False

        if transcript.status != TRANSCRIPT_STATUS_PROCESSING:
            return False

        # ---------------------------------------------------------
        # 2. Load recording
        # ---------------------------------------------------------

        recording = db.get(
            MeetingRecording,
            transcript.recording_id,
        )

        if recording is None:
            raise ValueError(
                "Transcript recording does not exist."
            )

        if recording.status != "completed":
            raise ValueError(
                "Transcript requires a completed recording."
            )

        # ---------------------------------------------------------
        # 3. Resolve speaker
        # ---------------------------------------------------------

        speaker = identify_recording_speaker(
            participant_name=recording.participant_name,
        )

        # ---------------------------------------------------------
        # 4. Locate encrypted recording
        # ---------------------------------------------------------

        encrypted_path = _recording_path(
            recording,
            storage_root,
        )

        # ---------------------------------------------------------
        # 5. Decrypt recording temporarily
        # ---------------------------------------------------------

        plaintext_path = decrypt_recording_to_temporary_file(
            encrypted_path,
            temporary_directory=temporary_directory,
        )

        # ---------------------------------------------------------
        # 6. Run Whisper
        # ---------------------------------------------------------

        segment_iterator, _info = model.transcribe(
            str(plaintext_path),
            language="en",
        )

        # ---------------------------------------------------------
        # 7. Build transcript segments
        # ---------------------------------------------------------

        segments: list[dict[str, object]] = []
        text_parts: list[str] = []

        for segment in segment_iterator:

            segment_text = str(segment.text).strip()

            segment_data = {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment_text,
                "speaker": speaker.name,
                "flagged_for_correction": speaker.flagged_for_correction,
            }

            segments.append(segment_data)

            if segment_text:
                text_parts.append(segment_text)

        # ---------------------------------------------------------
        # 8. Persist transcript
        # ---------------------------------------------------------

        transcript.language = "en"

        transcript.transcript_text = " ".join(
            text_parts
        )

        transcript.segments = segments

        transcript.status = TRANSCRIPT_STATUS_COMPLETED

        transcript.error_message = None

        transcript.completed_at = datetime.now(
            timezone.utc
        )

        db.commit()

        logger.info(
            "Transcript %s completed. Speaker=%s",
            transcript_id,
            speaker.name,
        )

        return True

    except Exception as error:

        logger.exception(
            "Transcription failed for transcript %s",
            transcript_id,
        )

        _persist_failure(
            db,
            transcript_id,
            error,
        )

        return False

    finally:

        # ---------------------------------------------------------
        # 9. Always delete decrypted temporary media
        # ---------------------------------------------------------

        if plaintext_path is not None:
            plaintext_path.unlink(
                missing_ok=True
            )


def run_worker(
    *,
    poll_interval_seconds: float = 2,
    session_factory: Callable[[], Session] = SessionLocal,
    model_factory: Callable[[], object] = create_whisper_model,
    storage_root: str | Path = RECORDING_STORAGE_DIR,
    stop_event: threading.Event | None = None,
) -> None:

    model = None

    while (
        stop_event is None
        or not stop_event.is_set()
    ):

        claim_session = session_factory()

        try:
            transcript_id = claim_pending_transcript(
                claim_session
            )

        finally:
            claim_session.close()

        if transcript_id is None:

            if stop_event is None:
                time.sleep(
                    poll_interval_seconds
                )
            else:
                stop_event.wait(
                    poll_interval_seconds
                )

            continue

        work_session = session_factory()

        try:

            if model is None:
                model = model_factory()

            process_claimed_transcript(
                work_session,
                transcript_id,
                model,
                storage_root=storage_root,
            )

        except Exception as error:

            logger.exception(
                "Unable to initialize transcription for %s",
                transcript_id,
            )

            _persist_failure(
                work_session,
                transcript_id,
                error,
            )

        finally:
            work_session.close()


if __name__ == "__main__":
    run_worker()