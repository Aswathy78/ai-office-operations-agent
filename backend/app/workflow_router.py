"""Authenticated workflow status and live meeting-progress updates."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.authentication import require_authenticated_principal
from app.auth_service import AuthenticatedPrincipal
from app.db.database import SessionLocal, get_db
from app.meeting.models import Meeting, MeetingAttendanceLog, MeetingParticipant, WorkflowRun
from app.workflow_service import (
    DeadLetterConflict,
    DeadLetterNotFound,
    dead_letter_counts,
    dead_letter_detail,
    list_dead_letters,
    resolve_dead_letter,
    workflow_response,
)

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


# ---------------------------------------------------------------------------
# Story 8.4 - dead-letter queue for administrators
# ---------------------------------------------------------------------------


def require_admin(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator can manage the workflow dead-letter queue.",
        )
    return principal


class DeadLetterActionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


@router.get("/dead-letter")
def get_dead_letter_queue(
    entry_status: str | None = Query(
        default="open",
        alias="status",
        description="open (default), retried, resumed, cancelled, or all",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthenticatedPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if entry_status not in (None, "all", "open", "retried", "resumed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status must be open, retried, resumed, cancelled or all.",
        )
    return {
        "entries": list_dead_letters(
            db, status=None if entry_status in (None, "all") else entry_status, limit=limit
        ),
        "counts": dead_letter_counts(db),
    }


@router.get("/dead-letter/{run_id}")
def get_dead_letter_detail(
    run_id: str,
    _: AuthenticatedPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return dead_letter_detail(db, run_id)
    except DeadLetterNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _act(
    action: str,
    run_id: str,
    body: DeadLetterActionRequest | None,
    principal: AuthenticatedPrincipal,
    db: Session,
) -> dict:
    try:
        resolve_dead_letter(
            db,
            run_id,
            action=action,
            actor_id=principal.user_id,
            note=body.note if body else None,
        )
    except DeadLetterNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except DeadLetterConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return dead_letter_detail(db, run_id)


@router.post("/dead-letter/{run_id}/retry")
def retry_dead_letter(
    run_id: str,
    body: DeadLetterActionRequest | None = None,
    principal: AuthenticatedPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Run the failed workflow again from its first step with a fresh attempt budget."""
    return _act("retry", run_id, body, principal, db)


@router.post("/dead-letter/{run_id}/resume")
def resume_dead_letter(
    run_id: str,
    body: DeadLetterActionRequest | None = None,
    principal: AuthenticatedPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Continue the failed workflow from its last checkpoint."""
    return _act("resume", run_id, body, principal, db)


@router.post("/dead-letter/{run_id}/cancel")
def cancel_dead_letter(
    run_id: str,
    body: DeadLetterActionRequest | None = None,
    principal: AuthenticatedPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Give up on the failed workflow; it ends as cancelled."""
    return _act("cancel", run_id, body, principal, db)
