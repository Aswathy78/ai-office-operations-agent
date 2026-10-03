"""Internal retention cleanup for persisted meeting recording files."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from app.meeting.models import (
    MeetingRecording,
    RECORDING_STATUS_CANCELLED,
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_FAILED,
)

logger = logging.getLogger(__name__)

RECORDING_STORAGE_DIR = Path(__file__).resolve().parents[2] / "storage" / "recordings"
RETENTION_TERMINAL_STATUSES = (
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_FAILED,
    RECORDING_STATUS_CANCELLED,
)


@dataclass(frozen=True)
class RetentionCleanupFailure:
    recording_id: str
    meeting_id: str
    reason: str


@dataclass
class RetentionCleanupReport:
    deleted_recording_ids: list[str] = field(default_factory=list)
    skipped_recording_ids: list[str] = field(default_factory=list)
    failures: list[RetentionCleanupFailure] = field(default_factory=list)


class UnsafeRecordingStorageUri(ValueError):
    """Raised when a storage URI is invalid or escapes recording storage."""


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _recording_file_path(
    recording: MeetingRecording,
    storage_root: Path,
) -> Path | None:
    if not recording.storage_uri:
        return None

    if "\\" in recording.storage_uri:
        raise UnsafeRecordingStorageUri("Storage URI contains an invalid separator")

    relative_path = PurePosixPath(recording.storage_uri)
    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 2
        or any(part in ("", ".", "..") for part in relative_path.parts)
        or ":" in recording.storage_uri
    ):
        raise UnsafeRecordingStorageUri("Storage URI is not a safe recording-relative path")

    meeting_directory, stored_filename = relative_path.parts
    if meeting_directory != recording.meeting_id:
        raise UnsafeRecordingStorageUri("Storage URI meeting ID does not match its recording")
    if not stored_filename.startswith(f"{recording.id}."):
        raise UnsafeRecordingStorageUri("Storage URI filename does not match its recording ID")

    root = storage_root.resolve()
    candidate = root.joinpath(*relative_path.parts)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(root):
        raise UnsafeRecordingStorageUri("Storage URI resolves outside recording storage")
    return candidate


def enforce_recording_retention(
    db: Session,
    *,
    now: datetime | None = None,
    storage_root: str | Path | None = None,
) -> RetentionCleanupReport:
    """Delete expired terminal recording files, then their database rows.

    This is an internal maintenance service. It does not decrypt files and is
    intentionally not bound to an HTTP endpoint or a scheduler in this step.
    A failed file or database operation leaves its recording row available for
    retry; failures for one recording do not prevent processing the others.
    """
    cutoff = _utc_datetime(now or datetime.now(timezone.utc))
    root = Path(storage_root) if storage_root is not None else RECORDING_STORAGE_DIR
    report = RetentionCleanupReport()

    terminal_recordings = (
        db.query(MeetingRecording)
        .filter(MeetingRecording.status.in_(RETENTION_TERMINAL_STATUSES))
        .order_by(MeetingRecording.id)
        .all()
    )

    for candidate in terminal_recordings:
        expires_at = candidate.retention_expires_at
        if expires_at is None or _utc_datetime(expires_at) > cutoff:
            report.skipped_recording_ids.append(candidate.id)
            continue

        recording_id = candidate.id
        meeting_id = candidate.meeting_id
        try:
            # Refresh the record before deleting in case its lifecycle changed
            # after the initial candidate query.
            recording = db.get(MeetingRecording, recording_id)
            if recording is None:
                continue
            if recording.status not in RETENTION_TERMINAL_STATUSES:
                report.skipped_recording_ids.append(recording_id)
                continue
            if (
                recording.retention_expires_at is None
                or _utc_datetime(recording.retention_expires_at) > cutoff
            ):
                report.skipped_recording_ids.append(recording_id)
                continue

            file_path = _recording_file_path(recording, root)
            if file_path is not None:
                file_path.unlink(missing_ok=True)

            db.delete(recording)
            db.commit()
            report.deleted_recording_ids.append(recording_id)
            logger.info(
                "Expired meeting recording removed",
                extra={
                    "recording_id": recording_id,
                    "meeting_id": meeting_id,
                    "cleanup_result": "deleted",
                },
            )
        except Exception as error:
            db.rollback()
            failure = RetentionCleanupFailure(
                recording_id=recording_id,
                meeting_id=meeting_id,
                reason=f"{type(error).__name__}: {error}",
            )
            report.failures.append(failure)
            logger.warning(
                "Meeting recording retention cleanup failed",
                extra={
                    "recording_id": recording_id,
                    "meeting_id": meeting_id,
                    "cleanup_result": "failed",
                    "error_type": type(error).__name__,
                },
            )

    return report