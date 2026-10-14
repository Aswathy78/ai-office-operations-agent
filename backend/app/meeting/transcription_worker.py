"""Database-backed Whisper worker for completed meeting recordings.

Run from the backend directory with:

    python -m app.meeting.transcription_worker
"""

from datetime import datetime, timezone
import ctypes
from importlib.util import find_spec
import logging
import os
from pathlib import Path, PurePosixPath
import sys
import threading
import time
from typing import Callable
from dotenv import load_dotenv

# Ensure backend .env is loaded
load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)
from app.meeting.action_item_service import generate_meeting_action_items
from app.meeting.summary_service import generate_meeting_summary
from sqlalchemy.orm import Session

from app.db.database import DATABASE_URL, SessionLocal
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

_CUDA_DLL_PACKAGE_DIRS = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")
_IDLE_LOG_INTERVAL_SECONDS = 30
_CUDA_DLL_DIRECTORY_HANDLES: list[object] = []
_CUDA_DLL_LIBRARY_HANDLES: list[object] = []
_REGISTERED_CUDA_DLL_DIRECTORIES: set[Path] = set()
_LOADED_CUDA_DLL_LIBRARIES: set[Path] = set()


def _configure_cuda_dll_directories(
    *,
    windows: bool | None = None,
) -> tuple[Path, ...]:
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return ()

    nvidia_spec = find_spec("nvidia")
    package_roots = nvidia_spec.submodule_search_locations if nvidia_spec else None
    if not package_roots:
        logger.warning("NVIDIA Python CUDA package directory was not found")
        return ()

    added_directories: list[Path] = []
    cublas_bin_directory: Path | None = None
    for package_root in package_roots:
        for package_name in _CUDA_DLL_PACKAGE_DIRS:
            directory = (Path(package_root) / package_name / "bin").resolve()
            if not directory.is_dir():
                logger.warning("NVIDIA CUDA DLL directory was not found: %s", directory)
                continue
            if directory in _REGISTERED_CUDA_DLL_DIRECTORIES:
                continue

            _CUDA_DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(directory)))
            _REGISTERED_CUDA_DLL_DIRECTORIES.add(directory)
            added_directories.append(directory)
            logger.info("Registered NVIDIA CUDA DLL directory: %s", directory)
            if package_name == "cublas":
                cublas_bin_directory = directory

    if cublas_bin_directory is None:
        cublas_directories = [
            (Path(package_root) / "cublas" / "bin").resolve()
            for package_root in package_roots
        ]
        cublas_bin_directory = next(
            (directory for directory in cublas_directories if directory.is_dir()),
            None,
        )
    if cublas_bin_directory is None:
        raise RuntimeError("NVIDIA cuBLAS DLL directory was not found.")

    cublas_libraries = sorted(cublas_bin_directory.glob("cublas64_*.dll"))
    if not cublas_libraries:
        raise RuntimeError(
            f"NVIDIA cuBLAS runtime DLL was not found in {cublas_bin_directory}."
        )

    cublas_library = cublas_libraries[0]
    if cublas_library not in _LOADED_CUDA_DLL_LIBRARIES:
        cublas_handle = ctypes.WinDLL(str(cublas_library))
        _CUDA_DLL_LIBRARY_HANDLES.append(cublas_handle)
        _LOADED_CUDA_DLL_LIBRARIES.add(cublas_library)
        logger.info("Loaded NVIDIA cuBLAS runtime DLL: %s", cublas_library)

    return tuple(added_directories)


def _load_whisper_model(device: str, compute_type: str):
    from faster_whisper import WhisperModel

    logger.info(
        "Whisper model loading model=small device=%s compute_type=%s",
        device,
        compute_type,
    )
    model = WhisperModel(
        "small",
        device=device,
        compute_type=compute_type,
    )
    logger.info(
        "Whisper model loaded device=%s compute_type=%s",
        device,
        compute_type,
    )
    return model


def create_whisper_model():
    """Load Whisper on CUDA, or on CPU when WHISPER_DEVICE=cpu.

    WHISPER_COMPUTE_TYPE overrides the device's default compute type.
    """
    device = os.environ.get("WHISPER_DEVICE", "cuda").strip().lower() or "cuda"
    compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "").strip()

    if device == "cpu":
        return _load_whisper_model("cpu", compute_type or "int8")
    if device != "cuda":
        raise ValueError("WHISPER_DEVICE must be 'cuda' or 'cpu'.")

    _configure_cuda_dll_directories()
    return _load_whisper_model("cuda", compute_type or "float16")


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
            word_timestamps=True,
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
            word_timestamps = [
                {
                    "start": float(word.start),
                    "end": float(word.end),
                    "word": str(word.word),
                }
                for word in (getattr(segment, "words", None) or [])
            ]
            if word_timestamps:
                segment_data["words"] = word_timestamps

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
        try:
            generate_meeting_summary(db, transcript.id)
        except Exception:
            logger.error(
                "Meeting summary generation failed for transcript %s",
                transcript.id,
            )
        try:
            generate_meeting_action_items(db, transcript.id)
        except Exception:
            logger.error(
                "Meeting action item extraction failed for transcript %s",
                transcript.id,
            )

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

    load_dotenv(
        dotenv_path=Path(__file__).resolve().parents[2] / ".env",
        override=False,
    )

    logger.info(
        "Transcription worker started database=%s poll_interval_seconds=%s",
        DATABASE_URL,
        poll_interval_seconds,
    )
    model = None
    last_idle_log_at = 0.0

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
            now = time.monotonic()
            if now - last_idle_log_at >= _IDLE_LOG_INTERVAL_SECONDS:
                logger.info(
                    "No pending transcripts; continuing to poll every %.1f seconds",
                    poll_interval_seconds,
                )
                last_idle_log_at = now

            if stop_event is None:
                time.sleep(
                    poll_interval_seconds
                )
            else:
                stop_event.wait(
                    poll_interval_seconds
                )

            continue

        last_idle_log_at = 0.0
        logger.info("Claimed pending transcript %s", transcript_id)
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
                "Unable to initialize transcription for %s: %s",
                transcript_id,
                error,
            )

            _persist_failure(
                work_session,
                transcript_id,
                error,
            )

        finally:
            work_session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_worker()