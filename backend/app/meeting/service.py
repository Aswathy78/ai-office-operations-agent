from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import tempfile
from typing import BinaryIO
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.meeting.models import (
    Meeting,
    MeetingParticipant,
    MeetingAttendanceLog,
    MeetingGuestRequest,
    MeetingRecording,
    MeetingRecordingConsent,
    GUEST_STATUS_PENDING,
    GUEST_STATUS_ADMITTED,
    GUEST_STATUS_REJECTED,
    RECORDING_CONSENT_STATUS_CONSENTED,
    RECORDING_STATUS_PENDING,
    RECORDING_STATUS_RECORDING,
    RECORDING_STATUS_PROCESSING,
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_FAILED,
    RECORDING_STATUS_CANCELLED,
    RECORDING_RETENTION_DAYS,
)
from app.meeting.schemas import AttendanceJoinRequest, GuestJoinRequest, MeetingCreate
from app.meeting.recording_encryption import (
    RecordingEncryptionConfigurationError,
    encrypt_recording_file,
)
from app.meeting.signaling import signaling_manager

# Statuses that mean the meeting can no longer be joined
TERMINAL_STATUSES = ("completed", "cancelled")
# Statuses that indicate the meeting is live / was started
ACTIVE_STATUSES = ("active", "instant")
RECORDING_STORAGE_DIR = Path(__file__).resolve().parents[2] / "storage" / "recordings"
RECORDING_MEDIA_TYPES = {
    "audio/webm": {".webm"},
    "video/webm": {".webm"},
    "audio/ogg": {".ogg", ".oga"},
    "audio/mp4": {".mp4", ".m4a"},
    "audio/mpeg": {".mp3"},
    "audio/wav": {".wav"},
    "audio/x-wav": {".wav"},
}




def create_meeting(
    db: Session,
    meeting_data: MeetingCreate,
    organizer_id: str,
) -> Meeting:

    meeting_id = str(uuid.uuid4())
    join_url = f"http://localhost:5173/meetings/{meeting_id}"
    now = datetime.now(timezone.utc)

    if meeting_data.meeting_type == "instant":
        start_at = now
        end_at = now + timedelta(hours=4)
        meeting_status = "instant"
    else:
        start_at = meeting_data.start_at
        end_at = meeting_data.end_at
        meeting_status = "scheduled"

    meeting = Meeting(
        id=meeting_id,
        organizer_id=organizer_id,
        title=meeting_data.title,
        agenda=meeting_data.agenda,
        start_at=start_at,
        end_at=end_at,
        join_url=join_url,
        status=meeting_status,
    )

    db.add(meeting)

    for participant in meeting_data.participants:
        participant_record = MeetingParticipant(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            user_id=participant.user_id,
            email=participant.email,
            role="participant",
        )

        db.add(participant_record)

    db.commit()
    db.refresh(meeting)

    return meeting

def get_user_meetings(
    db: Session,
    user_id: str | None = None,
    email: str | None = None,
) -> list[Meeting]:

    query = (
        db.query(Meeting)
        .outerjoin(
            MeetingParticipant,
            Meeting.id == MeetingParticipant.meeting_id,
        )
    )

    if email and user_id:
        query = query.filter(
            (Meeting.organizer_id == user_id)
            | (MeetingParticipant.email == email)
        )
    elif email:
        query = query.filter(
            MeetingParticipant.email == email
        )
    elif user_id:
        query = query.filter(
            (Meeting.organizer_id == user_id)
            | (MeetingParticipant.user_id == user_id)
        )

    meetings = query.distinct().all()

    # Authoritative status check: repair any stale meetings that have completed or expired
    for m in meetings:
        if m.status not in TERMINAL_STATUSES:
            _try_complete_meeting(db, m.id)

    return meetings


def get_meeting_by_id(
    db: Session,
    meeting_id: str,
) -> Meeting | None:
    return db.query(Meeting).filter(Meeting.id == meeting_id).first()


def is_meeting_expired(
    meeting: Meeting,
    reference_time: datetime | None = None,
) -> bool:
    if meeting.status in ["completed", "cancelled"]:
        return True

    # Instant meetings are active for 4h from creation; treat as live.
    # 'active' is a transitional status set externally.
    if meeting.status in ["instant", "active"]:
        now = reference_time or datetime.now(timezone.utc)
        end_at = meeting.end_at
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now > end_at

    now = reference_time or datetime.now(timezone.utc)
    end_at = meeting.end_at

    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return now > end_at


def record_participant_join(
    db: Session,
    meeting_id: str,
    participant_data: AttendanceJoinRequest,
) -> MeetingAttendanceLog:
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This meeting has already completed and can no longer be joined",
        )

    if is_meeting_expired(meeting):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Meeting has expired",
        )

    # Mark meeting as active when the first participant joins (was scheduled/instant)
    if meeting.status in ("scheduled", "instant"):
        meeting.status = "active"
        db.commit()

    guest_request = None
    participant_name = participant_data.participant_name
    participant_email = participant_data.participant_email
    if participant_data.guest_request_id:
        guest_request = get_guest_request_by_id(db, participant_data.guest_request_id)
        if not guest_request or guest_request.meeting_id != meeting_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Guest request not found",
            )
        if guest_request.status != GUEST_STATUS_ADMITTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Guest request has not been admitted",
            )
        participant_name = guest_request.guest_name
        participant_email = guest_request.guest_email

    active_query = db.query(MeetingAttendanceLog).filter(
        MeetingAttendanceLog.meeting_id == meeting_id,
        MeetingAttendanceLog.left_at.is_(None),
    )

    if guest_request and guest_request.attendance_log_id:
        existing = active_query.filter(
            MeetingAttendanceLog.id == guest_request.attendance_log_id
        ).first()
    elif guest_request and participant_email:
        existing = active_query.filter(
            MeetingAttendanceLog.participant_email == participant_email
        ).first()
    elif guest_request:
        existing = active_query.filter(
            MeetingAttendanceLog.participant_name == participant_name
        ).first()
    elif participant_data.user_id:
        existing = active_query.filter(
            MeetingAttendanceLog.user_id == participant_data.user_id
        ).first()
    elif participant_email:
        existing = active_query.filter(
            MeetingAttendanceLog.participant_email == participant_email
        ).first()
    else:
        existing = active_query.filter(
            MeetingAttendanceLog.participant_name == participant_name
        ).first()

    if existing:
        if guest_request and guest_request.attendance_log_id != existing.id:
            guest_request.attendance_log_id = existing.id
            db.commit()
        return existing

    attendance = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name=participant_name,
        participant_email=participant_email,
        user_id=None if guest_request else participant_data.user_id,
        is_external_guest=participant_data.is_external_guest or guest_request is not None,
        joined_at=datetime.now(timezone.utc),
        left_at=None,
    )

    db.add(attendance)
    if guest_request:
        db.flush()
        guest_request.attendance_log_id = attendance.id
    db.commit()
    db.refresh(attendance)

    return attendance


def record_participant_leave(
    db: Session,
    meeting_id: str,
    attendance_id: str,
) -> MeetingAttendanceLog:
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )
    attendance = (
        db.query(MeetingAttendanceLog)
        .filter(MeetingAttendanceLog.id == attendance_id)
        .first()
    )
    if not attendance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attendance record not found",
        )
    if attendance.meeting_id != meeting_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attendance record does not belong to this meeting",
        )
    if attendance.left_at is None:
        attendance.left_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(attendance)
    # Attempt to auto-complete the meeting if no participants remain (REST-path fallback).
    # The WebSocket signaling path also calls this in its finally block.
    _try_complete_meeting(db, meeting_id)
    return attendance


def request_guest_join(
    db: Session,
    meeting_id: str,
    guest_data: GuestJoinRequest,
) -> MeetingGuestRequest:
    """Create a pending lobby request for an external guest.

    If the same guest (matched by email, or name when email is absent) already
    has a pending request, return it to make retries idempotent.
    """
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )
    if meeting.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This meeting has already completed and can no longer be joined",
        )
    if is_meeting_expired(meeting):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Meeting has expired",
        )

    pending_query = db.query(MeetingGuestRequest).filter(
        MeetingGuestRequest.meeting_id == meeting_id,
        MeetingGuestRequest.status == GUEST_STATUS_PENDING,
    )
    if guest_data.guest_email:
        existing = pending_query.filter(
            MeetingGuestRequest.guest_email == guest_data.guest_email
        ).first()
    else:
        existing = pending_query.filter(
            MeetingGuestRequest.guest_name == guest_data.guest_name
        ).first()
    if existing:
        return existing

    guest_request = MeetingGuestRequest(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        guest_name=guest_data.guest_name,
        guest_email=guest_data.guest_email,
        status=GUEST_STATUS_PENDING,
        requested_at=datetime.now(timezone.utc),
    )
    db.add(guest_request)
    db.commit()
    db.refresh(guest_request)
    return guest_request


def get_guest_request_by_id(
    db: Session,
    request_id: str,
) -> MeetingGuestRequest | None:
    return (
        db.query(MeetingGuestRequest)
        .filter(MeetingGuestRequest.id == request_id)
        .first()
    )


def get_pending_guest_requests(
    db: Session,
    meeting_id: str,
) -> list[MeetingGuestRequest]:
    """Return all pending lobby requests for a meeting (organizer view)."""
    return (
        db.query(MeetingGuestRequest)
        .filter(
            MeetingGuestRequest.meeting_id == meeting_id,
            MeetingGuestRequest.status == GUEST_STATUS_PENDING,
        )
        .all()
    )


def decide_guest_request(
    db: Session,
    meeting_id: str,
    request_id: str,
    action: str,
    organizer_id: str,
) -> MeetingGuestRequest:
    """Admit or reject a pending lobby request.

    Admission records permission only; attendance is created by the existing
    participant join flow when the guest actually enters the meeting.
    """
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != organizer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can decide guest requests",
        )

    guest_req = get_guest_request_by_id(db, request_id)
    if not guest_req or guest_req.meeting_id != meeting_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Guest request not found",
        )

    if guest_req.status != GUEST_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Guest request is already '{guest_req.status}'",
        )

    if action not in ("admit", "reject"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action must be 'admit' or 'reject'",
        )

    now = datetime.now(timezone.utc)
    guest_req.decided_at = now

    if action == "admit":
        guest_req.status = GUEST_STATUS_ADMITTED
    else:
        guest_req.status = GUEST_STATUS_REJECTED

    db.commit()
    db.refresh(guest_req)
    return guest_req


def record_meeting_recording_consent(
    db: Session,
    meeting_id: str,
    attendance_id: str,
    consent_status: str,
) -> MeetingRecordingConsent:
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )
    if meeting.status in TERMINAL_STATUSES or is_meeting_expired(meeting):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recording consent cannot be changed for a completed or expired meeting",
        )

    attendance = (
        db.query(MeetingAttendanceLog)
        .filter(
            MeetingAttendanceLog.id == attendance_id,
            MeetingAttendanceLog.meeting_id == meeting_id,
        )
        .first()
    )
    if not attendance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attendance record not found for this meeting",
        )
    if attendance.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an active participant can record consent",
        )

    consent = (
        db.query(MeetingRecordingConsent)
        .filter(
            MeetingRecordingConsent.meeting_id == meeting_id,
            MeetingRecordingConsent.attendance_id == attendance_id,
        )
        .first()
    )
    now = datetime.now(timezone.utc)
    if consent is None:
        consent = MeetingRecordingConsent(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            attendance_id=attendance_id,
            consent_status=consent_status,
            consented_at=(
                now
                if consent_status == RECORDING_CONSENT_STATUS_CONSENTED
                else None
            ),
            created_at=now,
        )
        db.add(consent)
    elif not (
        consent.consent_status == consent_status
        and consent.revoked_at is None
    ):
        consent.consent_status = consent_status
        consent.consented_at = (
            now if consent_status == RECORDING_CONSENT_STATUS_CONSENTED else None
        )
        consent.revoked_at = None

    db.commit()
    db.refresh(consent)
    return consent


def _get_recording_for_organizer(
    db: Session,
    meeting_id: str,
    organizer_id: str,
) -> Meeting:
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )
    if meeting.organizer_id != organizer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can manage recording",
        )
    return meeting


def _latest_meeting_recording(
    db: Session,
    meeting_id: str,
) -> MeetingRecording | None:
    return (
        db.query(MeetingRecording)
        .filter(MeetingRecording.meeting_id == meeting_id)
        .order_by(MeetingRecording.created_at.desc(), MeetingRecording.id.desc())
        .first()
    )


def get_meeting_recording(
    db: Session,
    meeting_id: str,
    organizer_id: str,
) -> MeetingRecording:
    _get_recording_for_organizer(db, meeting_id, organizer_id)
    recording = _latest_meeting_recording(db, meeting_id)
    if recording is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recording not found",
        )
    return recording


def start_meeting_recording(
    db: Session,
    meeting_id: str,
    organizer_id: str,
) -> MeetingRecording:
    meeting = _get_recording_for_organizer(db, meeting_id, organizer_id)
    if meeting.status in TERMINAL_STATUSES or is_meeting_expired(meeting):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recording cannot start for a completed or expired meeting",
        )

    active_attendees = (
        db.query(MeetingAttendanceLog)
        .filter(
            MeetingAttendanceLog.meeting_id == meeting_id,
            MeetingAttendanceLog.left_at.is_(None),
        )
        .all()
    )
    if not active_attendees:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="At least one active participant is required to start recording",
        )

    active_attendance_ids = {attendee.id for attendee in active_attendees}
    consents = (
        db.query(MeetingRecordingConsent)
        .filter(
            MeetingRecordingConsent.meeting_id == meeting_id,
            MeetingRecordingConsent.attendance_id.in_(active_attendance_ids),
        )
        .all()
    )
    consent_by_attendance = {consent.attendance_id: consent for consent in consents}
    if any(
        attendance_id not in consent_by_attendance
        or consent_by_attendance[attendance_id].consent_status
        != RECORDING_CONSENT_STATUS_CONSENTED
        or consent_by_attendance[attendance_id].revoked_at is not None
        for attendance_id in active_attendance_ids
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recording requires explicit consent from all active participants",
        )

    recording = _latest_meeting_recording(db, meeting_id)
    if recording and recording.status == RECORDING_STATUS_RECORDING:
        return recording
    if recording and recording.status == RECORDING_STATUS_PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recording is still processing",
        )

    now = datetime.now(timezone.utc)
    if recording is None or recording.status in (
        RECORDING_STATUS_COMPLETED,
        RECORDING_STATUS_FAILED,
        RECORDING_STATUS_CANCELLED,
    ):
        recording = MeetingRecording(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            status=RECORDING_STATUS_PENDING,
            created_at=now,
            retention_expires_at=now + timedelta(days=RECORDING_RETENTION_DAYS),
        )
        db.add(recording)
        db.flush()
    elif recording.status != RECORDING_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot start recording from '{recording.status}' status",
        )

    recording.status = RECORDING_STATUS_RECORDING
    recording.started_at = now
    recording.completed_at = None
    recording.error_message = None
    db.commit()
    db.refresh(recording)
    return recording


def stop_meeting_recording(
    db: Session,
    meeting_id: str,
    organizer_id: str,
) -> MeetingRecording:
    _get_recording_for_organizer(db, meeting_id, organizer_id)
    recording = _latest_meeting_recording(db, meeting_id)
    if recording is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No recording is available to stop",
        )
    if recording.status == RECORDING_STATUS_PROCESSING:
        return recording
    if recording.status != RECORDING_STATUS_RECORDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot stop recording from '{recording.status}' status",
        )

    recording.status = RECORDING_STATUS_PROCESSING
    db.commit()
    db.refresh(recording)
    return recording


def upload_meeting_recording(
    db: Session,
    meeting_id: str,
    organizer_id: str,
    upload: BinaryIO,
    filename: str | None,
    content_type: str | None,
) -> dict[str, str | int]:
    _get_recording_for_organizer(db, meeting_id, organizer_id)
    recording = _latest_meeting_recording(db, meeting_id)
    if recording is None or recording.status != RECORDING_STATUS_PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A stopped recording in processing state is required before upload",
        )

    if not filename or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A safe recording filename is required",
        )
    extension = Path(filename).suffix.lower()
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    valid_extensions = RECORDING_MEDIA_TYPES.get(normalized_type)
    if not valid_extensions or extension not in valid_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported recording filename or audio content type",
        )

    try:
        meeting_component = str(uuid.UUID(meeting_id))
        recording_component = str(uuid.UUID(recording.id))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid meeting or recording identifier",
        ) from None

    storage_root = Path(RECORDING_STORAGE_DIR).resolve()
    meeting_directory = (storage_root / meeting_component).resolve()
    if not meeting_directory.is_relative_to(storage_root):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid recording storage location",
        )
    meeting_directory.mkdir(parents=True, exist_ok=True)

    target_path = meeting_directory / f"{recording_component}{extension}"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{recording_component}.",
        suffix=".plaintext.tmp",
        dir=meeting_directory,
    )
    os.close(descriptor)
    plaintext_path = Path(temporary_name)
    encrypted_path: Path | None = None
    size_bytes = 0
    try:
        encrypted_descriptor, encrypted_name = tempfile.mkstemp(
            prefix=f".{recording_component}.",
            suffix=".encrypted.tmp",
            dir=meeting_directory,
        )
        os.close(encrypted_descriptor)
        encrypted_path = Path(encrypted_name)
        with plaintext_path.open("wb") as destination:
            while chunk := upload.read(1024 * 1024):
                size_bytes += len(chunk)
                destination.write(chunk)
        if size_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Recording upload cannot be empty",
            )
        encrypt_recording_file(plaintext_path, encrypted_path)
        os.replace(encrypted_path, target_path)
    except RecordingEncryptionConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    except HTTPException:
        raise
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to encrypt or persist recording file",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to encrypt or persist recording file",
        ) from error
    finally:
        plaintext_path.unlink(missing_ok=True)
        if encrypted_path is not None:
            encrypted_path.unlink(missing_ok=True)

    storage_uri = f"{meeting_component}/{recording_component}{extension}"
    previous_uri = recording.storage_uri
    recording.storage_uri = storage_uri
    try:
        db.commit()
    except Exception as error:
        db.rollback()
        if previous_uri != storage_uri:
            target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to persist recording metadata",
        ) from error

    if previous_uri and previous_uri != storage_uri:
        previous_path = (storage_root / previous_uri).resolve()
        if previous_path.is_relative_to(storage_root) and previous_path.is_file():
            previous_path.unlink(missing_ok=True)

    return {
        "recording_id": recording.id,
        "status": recording.status,
        "storage_uri": storage_uri,
        "filename": target_path.name,
        "content_type": normalized_type,
        "size_bytes": size_bytes,
    }


# ---------------------------------------------------------------------------
# Meeting lifecycle helpers
# ---------------------------------------------------------------------------


def _try_complete_meeting(db: Session, meeting_id: str) -> bool:
    """Auto-complete the meeting if there are no open attendance records.

    Intended to be called as a best-effort finalisation; any exception is
    swallowed so that the caller (leave endpoint / WS finally block) is not
    disrupted.
    """
    try:
        return complete_meeting_if_empty(db, meeting_id)
    except Exception:
        return False


def complete_meeting_if_empty(db: Session, meeting_id: str) -> bool:
    """Mark the meeting as 'completed' when all participants have left or when expired.

    Rules:
    - Skip if the meeting does not exist or is already in a terminal state.
    - Check active WebSocket peers in signaling_manager. If active peers are connected,
      the meeting is live and cannot be auto-completed.
    - If no active peers are connected:
      - Check if meeting has expired (is_meeting_expired). If expired, mark completed.
      - Check if meeting was attended/started (total_logged > 0 or meeting.status == "active"):
        If attended and everyone left, finalize open attendance logs and mark completed.
      - If completed: stamp open attendance logs with now, set status='completed',
        record end_at = min(end_at, now), and commit.
    """
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        return False

    if meeting.status in TERMINAL_STATUSES:
        return False

    # If any WebSocket connections are active in the room, it is live
    active_peers = signaling_manager.get_room_participants(meeting_id)
    if active_peers:
        return False

    now = datetime.now(timezone.utc)
    expired = is_meeting_expired(meeting, reference_time=now)

    total_logged = (
        db.query(MeetingAttendanceLog)
        .filter(MeetingAttendanceLog.meeting_id == meeting_id)
        .count()
    )

    should_complete = False
    if expired:
        should_complete = True
    elif total_logged > 0 or meeting.status == "active":
        should_complete = True

    if not should_complete:
        return False

    # Finalize any open attendance records
    open_records = (
        db.query(MeetingAttendanceLog)
        .filter(
            MeetingAttendanceLog.meeting_id == meeting_id,
            MeetingAttendanceLog.left_at.is_(None),
        )
        .all()
    )
    for rec in open_records:
        rec.left_at = now

    meeting.status = "completed"

    end_at_dt = meeting.end_at
    if end_at_dt.tzinfo is None:
        end_at_dt = end_at_dt.replace(tzinfo=timezone.utc)
    if end_at_dt > now:
        meeting.end_at = now

    db.commit()
    return True


def force_complete_meeting(db: Session, meeting_id: str, organizer_id: str) -> Meeting:
    """Forcibly end the meeting (host clicked 'End meeting for everyone').

    Returns the updated Meeting object.
    """
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != organizer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can end the meeting",
        )

    if meeting.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Meeting has already ended",
        )

    now = datetime.now(timezone.utc)
    # Stamp all open attendance records
    open_records = (
        db.query(MeetingAttendanceLog)
        .filter(
            MeetingAttendanceLog.meeting_id == meeting_id,
            MeetingAttendanceLog.left_at.is_(None),
        )
        .all()
    )
    for rec in open_records:
        rec.left_at = now

    meeting.status = "completed"

    end_at_dt = meeting.end_at
    if end_at_dt.tzinfo is None:
        end_at_dt = end_at_dt.replace(tzinfo=timezone.utc)
    if end_at_dt > now:
        meeting.end_at = now

    db.commit()
    db.refresh(meeting)
    return meeting


def delete_meeting(db: Session, meeting_id: str, organizer_id: str) -> None:
    """Delete a completed or cancelled meeting and all child records.

    Only the organizer may delete their meeting, and only once it is completed.
    """
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != organizer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can delete this meeting",
        )

    # Authoritative status repair before evaluating deletion permission
    complete_meeting_if_empty(db, meeting_id)
    db.refresh(meeting)

    if meeting.status not in TERMINAL_STATUSES:
        active_peers = signaling_manager.get_room_participants(meeting_id)
        if active_peers or meeting.status in ("active", "instant"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete an active meeting in progress",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only completed or cancelled meetings can be deleted",
        )

    # Explicitly remove child records to avoid FK / orphan issues
    db.query(MeetingRecordingConsent).filter(
        MeetingRecordingConsent.meeting_id == meeting_id
    ).delete(synchronize_session=False)

    db.query(MeetingRecording).filter(
        MeetingRecording.meeting_id == meeting_id
    ).delete(synchronize_session=False)

    db.query(MeetingGuestRequest).filter(
        MeetingGuestRequest.meeting_id == meeting_id
    ).delete(synchronize_session=False)

    db.query(MeetingAttendanceLog).filter(
        MeetingAttendanceLog.meeting_id == meeting_id
    ).delete(synchronize_session=False)

    db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting_id
    ).delete(synchronize_session=False)

    db.delete(meeting)
    db.commit()
