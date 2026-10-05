import json
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response

from sqlalchemy.orm import Session

from app.db.database import get_db
from app.meeting.models import MeetingAttendanceLog
from app.meeting.schemas import (
    AttendanceJoinRequest,
    AttendanceReportRow,
    AttendanceResponse,
    GuestDecisionRequest,
    GuestJoinRequest,
    GuestRequestResponse,
    MeetingCreate,
    MeetingReportResponse,
    MeetingResponse,
    MeetingValidationResponse,
    MeetingRecordingUploadResponse,
    ParticipantTrackRegisterResponse,
    ParticipantTracksRegisterRequest,
    RecordingConsentRequest,
    RecordingConsentResponse,
    MeetingRecordingResponse,
)
from app.meeting.service import (
    TERMINAL_STATUSES,
    _try_complete_meeting,
    create_meeting,
    decide_guest_request,
    delete_meeting,
    force_complete_meeting,
    get_guest_request_by_id,
    get_meeting_by_id,
    get_pending_guest_requests,
    get_user_meetings,
    is_meeting_expired,
    record_participant_join,
    record_participant_leave,
    record_meeting_recording_consent,
    get_meeting_recording,
    start_meeting_recording,
    stop_meeting_recording,
    upload_meeting_recording,
    register_participant_track_recordings,
    upload_participant_track_recording,
    request_guest_join,
)
from app.meeting.signaling import signaling_manager



router = APIRouter(
    prefix="/api/v1/meetings",
    tags=["Meetings"],
)
DEMO_ORGANIZER_ID = "demo-organizer"


@router.post("/", response_model=MeetingResponse)
def create_new_meeting(
    meeting: MeetingCreate,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    created = create_meeting(
        db=db,
        meeting_data=meeting,
        organizer_id=organizer_id,
    )
    return MeetingResponse(
        id=created.id,
        title=created.title,
        agenda=created.agenda,
        start_at=created.start_at,
        end_at=created.end_at,
        join_url=created.join_url,
        status=created.status,
        organizer_id=created.organizer_id,
    )

@router.get("/", response_model=list[MeetingResponse])
def get_meetings(
    db: Session = Depends(get_db),
    email: str | None = None,
    user_id: str | None = None,
):
    meetings = get_user_meetings(
        db=db,
        user_id=user_id or DEMO_ORGANIZER_ID,
        email=email,
    )
    return [
        MeetingResponse(
            id=m.id,
            title=m.title,
            agenda=m.agenda,
            start_at=m.start_at,
            end_at=m.end_at,
            join_url=m.join_url,
            status=m.status,
            organizer_id=m.organizer_id,
        )
        for m in meetings
    ]


@router.get("/{meeting_id}/validate", response_model=MeetingValidationResponse)
def validate_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.status in TERMINAL_STATUSES:
        # Completed meetings return 410 so callers know they cannot join
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This meeting has already completed",
        )

    if is_meeting_expired(meeting):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Meeting has expired",
        )

    return MeetingValidationResponse(
        id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=meeting.start_at,
        end_at=meeting.end_at,
        status=meeting.status,
        organizer_id=meeting.organizer_id,
        is_valid=True,
        is_expired=False,
    )


@router.delete(
    "/{meeting_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Organizer deletes a completed meeting",
)
def delete_meeting_route(
    meeting_id: str,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    """Permanently delete a completed meeting and its attendance records.
    Only the organizer may call this, and only on completed/cancelled meetings.
    """
    delete_meeting(db=db, meeting_id=meeting_id, organizer_id=organizer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{meeting_id}/end",
    response_model=MeetingResponse,
    summary="Organizer forcibly ends the meeting for everyone",
)
async def end_meeting_for_everyone(
    meeting_id: str,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    """End the meeting immediately, stamping all attendance records and
    broadcasting a 'meeting_ended' signal to all connected participants.
    """
    meeting = force_complete_meeting(
        db=db,
        meeting_id=meeting_id,
        organizer_id=organizer_id,
    )
    # Broadcast to all connected WebSocket participants
    await signaling_manager.broadcast(
        meeting_id=meeting_id,
        message={
            "type": "meeting_ended",
            "sender_id": "server",
            "payload": {
                "meeting_id": meeting_id,
                "reason": "The host has ended the meeting.",
            },
        },
    )
    # Close participant WebSocket connections
    await signaling_manager.close_room(
        meeting_id=meeting_id,
        code=1000,
        reason="Meeting ended by host",
    )
    return MeetingResponse(
        id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=meeting.start_at,
        end_at=meeting.end_at,
        join_url=meeting.join_url,
        status=meeting.status,
        organizer_id=meeting.organizer_id,
    )


@router.get("/{meeting_id}", response_model=MeetingValidationResponse)
def get_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    return validate_meeting(meeting_id=meeting_id, db=db)


@router.post(
    "/{meeting_id}/attendance/join",
    response_model=AttendanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def join_meeting_attendance(
    meeting_id: str,
    participant_data: AttendanceJoinRequest,
    db: Session = Depends(get_db),
):
    return record_participant_join(
        db=db,
        meeting_id=meeting_id,
        participant_data=participant_data,
    )


@router.post(
    "/{meeting_id}/attendance/{attendance_id}/leave",
    response_model=AttendanceResponse,
    status_code=status.HTTP_200_OK,
)
def leave_meeting_attendance(
    meeting_id: str,
    attendance_id: str,
    db: Session = Depends(get_db),
):
    return record_participant_leave(
        db=db,
        meeting_id=meeting_id,
        attendance_id=attendance_id,
    )


@router.post(
    "/{meeting_id}/recording/consent",
    response_model=RecordingConsentResponse,
    summary="Record a participant's recording consent decision",
)
def recording_consent(
    meeting_id: str,
    consent_data: RecordingConsentRequest,
    db: Session = Depends(get_db),
):
    consent = record_meeting_recording_consent(
        db=db,
        meeting_id=meeting_id,
        attendance_id=consent_data.attendance_id,
        consent_status=consent_data.consent_status,
    )
    return RecordingConsentResponse(
        id=consent.id,
        meeting_id=consent.meeting_id,
        attendance_id=consent.attendance_id,
        consent_status=consent.consent_status,
        consented_at=consent.consented_at,
        revoked_at=consent.revoked_at,
        created_at=consent.created_at,
    )


def _recording_response(recording) -> MeetingRecordingResponse:
    return MeetingRecordingResponse(
        id=recording.id,
        meeting_id=recording.meeting_id,
        participant_id=recording.participant_id,
        status=recording.status,
        started_at=recording.started_at,
        completed_at=recording.completed_at,
        created_at=recording.created_at,
        retention_expires_at=recording.retention_expires_at,
        storage_uri=recording.storage_uri,
        error_message=recording.error_message,
    )


@router.get(
    "/{meeting_id}/recording",
    response_model=MeetingRecordingResponse,
    summary="Get the latest recording state",
)
def read_meeting_recording(
    meeting_id: str,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    recording = get_meeting_recording(db, meeting_id, organizer_id)
    return _recording_response(recording)


@router.post(
    "/{meeting_id}/recording/start",
    response_model=MeetingRecordingResponse,
    summary="Start recording state after all active participants consent",
)
def begin_meeting_recording(
    meeting_id: str,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    recording = start_meeting_recording(db, meeting_id, organizer_id)
    return _recording_response(recording)


@router.post(
    "/{meeting_id}/recording/stop",
    response_model=MeetingRecordingResponse,
    summary="Stop active recording state",
)
def end_meeting_recording(
    meeting_id: str,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    recording = stop_meeting_recording(db, meeting_id, organizer_id)
    return _recording_response(recording)


@router.post(
    "/{meeting_id}/recording/upload",
    response_model=MeetingRecordingUploadResponse,
    summary="Persist a completed meeting recording file",
)
def upload_meeting_recording_file(
    meeting_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    result = upload_meeting_recording(
        db=db,
        meeting_id=meeting_id,
        organizer_id=organizer_id,
        upload=file.file,
        filename=file.filename,
        content_type=file.content_type,
    )
    return MeetingRecordingUploadResponse(**result)


@router.post(
    "/{meeting_id}/recording/tracks",
    response_model=list[ParticipantTrackRegisterResponse],
    status_code=200,
    summary="Register participant-track recording rows (FR-8)",
)
def register_participant_tracks(
    meeting_id: str,
    body: ParticipantTracksRegisterRequest,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    """Creates one MeetingRecording row (status=processing) per submitted track.

    The frontend calls this after stopping the main recording and before
    uploading individual participant track blobs.  No MeetingTranscript rows
    are created here; they are created by the per-track upload endpoint.
    """
    rows = register_participant_track_recordings(
        db=db,
        meeting_id=meeting_id,
        organizer_id=organizer_id,
        tracks=[
            {
                "connection_id": track.connection_id,
                "participant_name": track.participant_name,
                "attendance_id": track.attendance_id,
            }
            for track in body.tracks
        ],
    )
    return [
        ParticipantTrackRegisterResponse(
            recording_id=row.id,
            connection_id=row.connection_id,
            participant_name=row.participant_name,
            status=row.status,
            meeting_id=row.meeting_id,
        )
        for row in rows
    ]


@router.post(
    "/{meeting_id}/recording/{recording_id}/upload",
    response_model=MeetingRecordingUploadResponse,
    summary="Upload a participant-specific audio track by explicit recording ID (FR-8)",
)
def upload_participant_track_file(
    meeting_id: str,
    recording_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    """Upload and encrypt a single participant audio track.

    The recording_id must have been previously registered via
    POST /recording/tracks and must belong to this meeting.
    A MeetingTranscript row is created automatically upon success.
    """
    result = upload_participant_track_recording(
        db=db,
        meeting_id=meeting_id,
        recording_id=recording_id,
        organizer_id=organizer_id,
        upload=file.file,
        filename=file.filename,
        content_type=file.content_type,
    )
    return MeetingRecordingUploadResponse(**result)


# -- Meeting Report (organizer only) ------------------------------------------


@router.get(
    "/{meeting_id}/report",
    response_model=MeetingReportResponse,
    summary="Organizer retrieves the attendance report for a meeting",
)
def get_meeting_report(
    meeting_id: str,
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
    db: Session = Depends(get_db),
):
    """Return attendance records for the meeting.

    The caller must supply their ``organizer_id``; if it does not match the
    stored organizer, a 403 is returned.  This is a lightweight server-side
    guard; a real deployment would use an authenticated session token.
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
            detail="Only the meeting organizer can access the report",
        )

    logs = (
        db.query(MeetingAttendanceLog)
        .filter(MeetingAttendanceLog.meeting_id == meeting_id)
        .order_by(MeetingAttendanceLog.joined_at)
        .all()
    )

    return MeetingReportResponse(
        meeting_id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=meeting.start_at,
        end_at=meeting.end_at,
        organizer_id=meeting.organizer_id,
        attendance=[
            AttendanceReportRow(
                participant_name=log.participant_name,
                participant_email=log.participant_email,
                user_id=log.user_id,
                is_external_guest=log.is_external_guest,
                joined_at=log.joined_at,
                left_at=log.left_at,
            )
            for log in logs
        ],
    )


# -- Guest Lobby REST Endpoints ------------------------------------------------


@router.post(
    "/{meeting_id}/lobby",
    response_model=GuestRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="External guest requests to join meeting lobby",
)
def guest_join_lobby(
    meeting_id: str,
    guest_data: GuestJoinRequest,
    db: Session = Depends(get_db),
):
    """An external guest submits their name/email and enters the lobby.
    Returns a lobby request record with ``status='pending'``.
    The organizer is notified via WebSocket.
    """
    guest_req = request_guest_join(db=db, meeting_id=meeting_id, guest_data=guest_data)
    return GuestRequestResponse(
        id=guest_req.id,
        meeting_id=guest_req.meeting_id,
        guest_name=guest_req.guest_name,
        guest_email=guest_req.guest_email,
        status=guest_req.status,
        requested_at=guest_req.requested_at,
        decided_at=guest_req.decided_at,
        attendance_log_id=guest_req.attendance_log_id,
    )


@router.get(
    "/{meeting_id}/lobby",
    response_model=list[GuestRequestResponse],
    summary="Organizer lists pending lobby requests",
)
def list_pending_guests(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    """Returns all pending guest requests for the given meeting."""
    pending = get_pending_guest_requests(db=db, meeting_id=meeting_id)
    return [
        GuestRequestResponse(
            id=r.id,
            meeting_id=r.meeting_id,
            guest_name=r.guest_name,
            guest_email=r.guest_email,
            status=r.status,
            requested_at=r.requested_at,
            decided_at=r.decided_at,
            attendance_log_id=r.attendance_log_id,
        )
        for r in pending
    ]


@router.post(
    "/{meeting_id}/lobby/{request_id}/decision",
    response_model=GuestRequestResponse,
    summary="Organizer admits or rejects a guest lobby request",
)
async def organizer_decide_guest(
    meeting_id: str,
    request_id: str,
    decision: GuestDecisionRequest,
    db: Session = Depends(get_db),
    organizer_id: str = Query(default=DEMO_ORGANIZER_ID),
):
    """Decide a guest request and notify its connected lobby socket.

    The caller supplies their ``organizer_id`` as a query parameter so the
    service layer can verify identity against the stored meeting organizer.
    """
    guest_req = decide_guest_request(
        db=db,
        meeting_id=meeting_id,
        request_id=request_id,
        action=decision.decision,
        organizer_id=organizer_id,
    )
    await signaling_manager.send_personal_message(
        meeting_id=f"lobby:{meeting_id}",
        target_id=guest_req.id,
        message={
            "type": (
                "guest_admitted"
                if guest_req.status == "admitted"
                else "guest_rejected"
            ),
            "guest_request_id": guest_req.id,
            "meeting_id": guest_req.meeting_id,
            "guest_name": guest_req.guest_name,
            "status": guest_req.status,
            "decided_at": guest_req.decided_at.isoformat()
            if guest_req.decided_at
            else None,
        },
    )
    return GuestRequestResponse(
        id=guest_req.id,
        meeting_id=guest_req.meeting_id,
        guest_name=guest_req.guest_name,
        guest_email=guest_req.guest_email,
        status=guest_req.status,
        requested_at=guest_req.requested_at,
        decided_at=guest_req.decided_at,
        attendance_log_id=guest_req.attendance_log_id,
    )


@router.websocket("/{meeting_id}/ws")
async def meeting_websocket_signaling(
    websocket: WebSocket,
    meeting_id: str,
    client_id: str | None = None,
    user_id: str | None = None,
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting not found",
        )
        return

    if meeting.status in TERMINAL_STATUSES:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting has already completed",
        )
        return

    if is_meeting_expired(meeting):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting has expired",
        )
        return

    # Release initial read transaction so SQLite does not hold a read lock during the WS loop
    db.commit()

    connection_id = await signaling_manager.connect(
        meeting_id=meeting_id,
        websocket=websocket,
        connection_id=client_id,
    )

    existing_participants = signaling_manager.get_room_participants(
        meeting_id=meeting_id,
        exclude_id=connection_id,
    )

    # Send welcome message with session ID and active peers
    await websocket.send_json({
        "type": "welcome",
        "sender_id": "server",
        "target_id": connection_id,
        "payload": {
            "connection_id": connection_id,
            "meeting_id": meeting_id,
            "participants": existing_participants,
            "organizer_id": meeting.organizer_id,
        },
    })

    # Broadcast participant_joined to existing room peers
    await signaling_manager.broadcast(
        meeting_id=meeting_id,
        message={
            "type": "participant_joined",
            "sender_id": connection_id,
            "payload": {
                "connection_id": connection_id,
            },
        },
        exclude_id=connection_id,
    )

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                message = json.loads(raw_text)
            except Exception:
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": "Invalid message: must be valid JSON",
                    },
                })
                continue

            if not isinstance(message, dict):
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": "Invalid message: root must be a JSON object",
                    },
                })
                continue

            msg_type = message.get("type")
            if not msg_type or not isinstance(msg_type, str):
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": "Invalid message: missing or invalid 'type' field",
                    },
                })
                continue

            if msg_type in ["offer", "answer", "ice_candidate"]:
                target_id = message.get("target_id")
                if not target_id or not isinstance(target_id, str):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"Missing or invalid 'target_id' for '{msg_type}' message",
                        },
                    })
                    continue

                if not signaling_manager.is_participant_in_meeting(meeting_id, target_id):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"Target participant '{target_id}' not found in meeting",
                        },
                    })
                    continue

                # Forward signaling payload to intended target peer
                await signaling_manager.send_personal_message(
                    meeting_id=meeting_id,
                    target_id=target_id,
                    message={
                        "type": msg_type,
                        "sender_id": connection_id,
                        "target_id": target_id,
                        "payload": message.get("payload", {}),
                    },
                )

            elif msg_type == "join":
                await websocket.send_json({
                    "type": "join_ack",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "connection_id": connection_id,
                        "meeting_id": meeting_id,
                    },
                })

            elif msg_type == "guest_request_notify":
                # Organizer-to-guest or guest-to-organizer lobby notification.
                # Simply broadcast to all room participants (organizer is in the room).
                await signaling_manager.broadcast(
                    meeting_id=meeting_id,
                    message={
                        "type": "guest_request_notify",
                        "sender_id": connection_id,
                        "payload": message.get("payload", {}),
                    },
                    exclude_id=connection_id,
                )

            elif msg_type in ("mute_participant", "ask_unmute"):
                # Only the meeting organizer may mute / ask to unmute another participant.
                # We verify organizer by comparing user_id (passed as WS query param) against
                # the stored organizer_id, falling back to connection_id for legacy clients.
                payload = message.get("payload", {})
                target_participant_id = payload.get("participant_id") if isinstance(payload, dict) else None

                # Reload meeting to get authoritative organizer_id
                current_meeting = get_meeting_by_id(db, meeting_id)
                if not current_meeting:
                    continue  # meeting disappeared

                # Accept either user_id match or connection_id match so both old and new clients work
                is_organizer = (
                    (user_id is not None and current_meeting.organizer_id == user_id)
                    or current_meeting.organizer_id == connection_id
                )

                if not is_organizer:
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": "Only the meeting organizer can mute participants",
                        },
                    })
                    continue

                if not target_participant_id or not isinstance(target_participant_id, str):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"{msg_type} requires a participant_id in payload",
                        },
                    })
                    continue

                if not signaling_manager.is_participant_in_meeting(meeting_id, target_participant_id):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"Target participant '{target_participant_id}' not found in meeting",
                        },
                    })
                    continue

                if msg_type == "mute_participant":
                    # Forward hard-mute command only to the targeted participant
                    await signaling_manager.send_personal_message(
                        meeting_id=meeting_id,
                        target_id=target_participant_id,
                        message={
                            "type": "mute_participant",
                            "sender_id": "server",
                            "target_id": target_participant_id,
                            "payload": {
                                "participant_id": target_participant_id,
                                "by_organizer": True,
                                "meeting_id": meeting_id,
                            },
                        },
                    )
                else:  # ask_unmute
                    # Forward a polite unmute request; the participant decides whether to unmute
                    await signaling_manager.send_personal_message(
                        meeting_id=meeting_id,
                        target_id=target_participant_id,
                        message={
                            "type": "ask_unmute",
                            "sender_id": "server",
                            "target_id": target_participant_id,
                            "payload": {
                                "participant_id": target_participant_id,
                                "meeting_id": meeting_id,
                            },
                        },
                    )

            elif msg_type == "participant_state":
                # Participant broadcasts their mic/camera state to the room.
                await signaling_manager.broadcast(
                    meeting_id=meeting_id,
                    message={
                        "type": "participant_state",
                        "sender_id": connection_id,
                        "payload": message.get("payload", {}),
                    },
                    exclude_id=connection_id,
                )

            elif msg_type == "leave":
                break

            else:
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": f"Unsupported message type: '{msg_type}'",
                    },
                })

    except WebSocketDisconnect:
        pass
    finally:
        signaling_manager.disconnect(meeting_id, connection_id)
        await signaling_manager.broadcast(
            meeting_id=meeting_id,
            message={
                "type": "participant_left",
                "sender_id": connection_id,
                "payload": {
                    "connection_id": connection_id,
                },
            },
            exclude_id=connection_id,
        )
        # Auto-complete the meeting when the last participant has left
        _try_complete_meeting(db, meeting_id)


# -- Lobby-only WebSocket (for guests waiting before admission) ----------------


@router.websocket("/{meeting_id}/lobby-ws")
async def lobby_websocket(
    websocket: WebSocket,
    meeting_id: str,
    guest_request_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Dedicated WebSocket channel bound to a validated guest request."""
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting not found",
        )
        return

    if is_meeting_expired(meeting):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting has expired",
        )
        return

    if not guest_request_id:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request ID is required",
        )
        return

    guest_request = get_guest_request_by_id(db, guest_request_id)
    if not guest_request or guest_request.meeting_id != meeting_id:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request not found for this meeting",
        )
        return

    if guest_request.status not in ("pending", "admitted", "rejected"):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request is not valid",
        )
        return

    # Use a lobby-namespaced room key so lobby connections are isolated
    lobby_room_id = f"lobby:{meeting_id}"
    if signaling_manager.is_participant_in_meeting(lobby_room_id, guest_request_id):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request already has an active lobby connection",
        )
        return

    connection_id = await signaling_manager.connect(
        meeting_id=lobby_room_id,
        websocket=websocket,
        connection_id=guest_request_id,
    )

    if guest_request.status in ("admitted", "rejected"):
        await signaling_manager.send_personal_message(
            meeting_id=lobby_room_id,
            target_id=connection_id,
            message={
                "type": (
                    "guest_admitted"
                    if guest_request.status == "admitted"
                    else "guest_rejected"
                ),
                "guest_request_id": guest_request.id,
                "meeting_id": guest_request.meeting_id,
                "guest_name": guest_request.guest_name,
                "status": guest_request.status,
                "decided_at": guest_request.decided_at.isoformat()
                if guest_request.decided_at
                else None,
            },
        )
        signaling_manager.disconnect(lobby_room_id, connection_id)
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        return

    await websocket.send_json({
        "type": "lobby_welcome",
        "sender_id": "server",
        "payload": {
            "connection_id": connection_id,
            "meeting_id": meeting_id,
            "message": "You are in the lobby. Waiting for the organizer to admit you.",
        },
    })

    # Notify the main meeting room that a new guest has arrived
    await signaling_manager.broadcast(
        meeting_id=meeting_id,
        message={
            "type": "guest_arrived",
            "sender_id": "server",
            "payload": {
                "guest_request_id": connection_id,
                "meeting_id": meeting_id,
            },
        },
    )

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                message = json.loads(raw_text)
            except Exception:
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "payload": {"detail": "Invalid JSON"},
                })
                continue

            msg_type = message.get("type", "")
            if msg_type == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "sender_id": "server",
                })
            elif msg_type == "leave":
                break

    except WebSocketDisconnect:
        pass
    finally:
        signaling_manager.disconnect(lobby_room_id, connection_id)