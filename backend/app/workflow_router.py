"""Authenticated workflow status and live meeting-progress updates."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.authentication import require_authenticated_principal
from app.auth_service import AuthenticatedPrincipal
from app.db.database import SessionLocal, get_db
from app.meeting.models import Meeting, MeetingAttendanceLog, MeetingParticipant, WorkflowRun
from app.workflow_service import workflow_response

router = APIRouter(prefix="/api/v1/workflows", tags=["Workflows"])


def _authorized_meeting(
    db: Session,
    meeting_id: str,
    principal: AuthenticatedPrincipal,
) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    if meeting.organizer_id == principal.user_id:
        return meeting
    attendance = (
        db.query(MeetingAttendanceLog.id)
        .filter(
            MeetingAttendanceLog.meeting_id == meeting_id,
            MeetingAttendanceLog.user_id == principal.user_id,
        )
        .first()
    )
    participant = (
        db.query(MeetingParticipant.id)
        .filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id == principal.user_id,
        )
        .first()
    )
    if attendance is None and participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    return meeting


def _latest_run(db: Session, meeting_id: str) -> WorkflowRun | None:
    return (
        db.query(WorkflowRun)
        .filter(WorkflowRun.meeting_id == meeting_id)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .first()
    )


@router.get("/meetings/{meeting_id}")
def get_meeting_workflow(
    meeting_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    _authorized_meeting(db, meeting_id, principal)
    run = _latest_run(db, meeting_id)
    return {"workflow": workflow_response(db, run) if run is not None else None}


@router.get("/meetings/{meeting_id}/events")
async def stream_meeting_workflow(
    meeting_id: str,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    _authorized_meeting(db, meeting_id, principal)

    async def events():
        last_signature: tuple | None = None
        last_heartbeat = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            with SessionLocal() as stream_db:
                run = _latest_run(stream_db, meeting_id)
                state = workflow_response(stream_db, run) if run is not None else None
                signature = (
                    run.updated_at.isoformat(),
                    tuple(
                        (step["id"], step["status"], step["updated_at"].isoformat())
                        for step in state["steps"]
                    ),
                ) if run is not None and state is not None else None
            if signature != last_signature:
                yield f"event: workflow\ndata: {json.dumps({'workflow': state}, default=str)}\n\n"
                last_signature = signature
                last_heartbeat = asyncio.get_running_loop().time()
            elif asyncio.get_running_loop().time() - last_heartbeat >= 15:
                yield ": keepalive\n\n"
                last_heartbeat = asyncio.get_running_loop().time()
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
