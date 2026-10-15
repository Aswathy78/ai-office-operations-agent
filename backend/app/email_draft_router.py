"""E-mail drafting from chat: preview, edit, approval and send (Story 4.3 / FR-20)."""

from datetime import datetime
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.email_draft_service import (
    EmailActor,
    EmailDraftConflict,
    EmailDraftDenied,
    EmailDraftNotFound,
    approval_recipient_threshold,
    create_draft_from_chat,
    decide_draft,
    discard_draft,
    get_draft,
    list_approval_queue,
    list_directory_groups,
    list_my_drafts,
    send_draft,
    submit_for_approval,
    update_draft,
)
from app.meeting.models import EmailDraft
from app.meeting.schemas import TaskActorRole

router = APIRouter(prefix="/api/v1/email-drafts", tags=["E-mail drafts"])


class EmailRecipient(BaseModel):
    name: str
    email: str
    internal: bool
    source: str


class EmailRecipientGroup(BaseModel):
    name: str
    count: int


class SensitiveFinding(BaseModel):
    type: str
    label: str
    severity: Literal["high", "medium", "low"]
    match: str
    field: Literal["subject", "body"]


class DeliveryResult(BaseModel):
    email: str
    status: Literal["sent", "failed"]
    error: str | None = None


class EmailDraftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_by_id: str
    created_by_name: str
    created_by_email: str | None
    created_by_role: str
    prompt: str
    source: Literal["template", "llm"]
    template_key: str | None
    generation_note: str | None
    subject: str
    body: str
    recipients: list[EmailRecipient]
    recipient_groups: list[EmailRecipientGroup]
    unresolved_recipients: list[str]
    recipient_count: int
    approval_required: bool
    approval_reasons: list[str]
    approval_threshold: int
    sensitive_findings: list[SensitiveFinding]
    status: str
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    delivery_results: list[DeliveryResult]
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EmailDraftCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    sender_name: str = Field(min_length=1, max_length=255)
    sender_email: str | None = Field(default=None, max_length=255)
    mode: Literal["auto", "template", "llm"] = "auto"


class EmailDraftUpdateRequest(BaseModel):
    subject: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=20000)
    recipient_emails: list[str] | None = None
    add_groups: list[str] | None = None


class EmailDraftSendRequest(BaseModel):
    confirm: bool = False
    acknowledge_sensitive: bool = False


class EmailDraftDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


def _actor(
    actor_id: str = Query(min_length=1),
    actor_role: TaskActorRole = Query(),
    actor_team: str | None = Query(default=None),
) -> EmailActor:
    return EmailActor(id=actor_id, role=actor_role, team=actor_team or None)


def _response(draft: EmailDraft) -> EmailDraftResponse:
    data = {column.name: getattr(draft, column.name) for column in EmailDraft.__table__.columns}
    return EmailDraftResponse(
        **data,
        recipient_count=len(draft.recipients),
        approval_threshold=approval_recipient_threshold(),
    )


def _run(action: Callable[[], EmailDraft]) -> EmailDraftResponse:
    try:
        return _response(action())
    except EmailDraftNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except EmailDraftDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except EmailDraftConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/directory/groups", response_model=list[EmailRecipientGroup])
def get_directory_groups(db: Session = Depends(get_db)):
    return list_directory_groups(db)


@router.post("", response_model=EmailDraftResponse, status_code=status.HTTP_201_CREATED)
def create_email_draft(
    payload: EmailDraftCreateRequest,
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    """Draft an e-mail from a chat message. Nothing is sent."""
    return _run(lambda: create_draft_from_chat(
        db, actor,
        prompt=payload.prompt,
        sender_name=payload.sender_name,
        sender_email=payload.sender_email,
        mode=payload.mode,
    ))


@router.get("", response_model=list[EmailDraftResponse])
def get_my_email_drafts(actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    return [_response(draft) for draft in list_my_drafts(db, actor)]


@router.get("/approvals", response_model=list[EmailDraftResponse])
def get_email_approval_queue(
    request_status: str | None = Query(default="pending_approval", alias="status"),
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    try:
        drafts = list_approval_queue(db, actor, request_status or None)
    except EmailDraftDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return [_response(draft) for draft in drafts]


@router.get("/{draft_id}", response_model=EmailDraftResponse)
def get_email_draft(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    return _run(lambda: get_draft(db, draft_id, actor))


@router.patch("/{draft_id}", response_model=EmailDraftResponse)
def update_email_draft(
    draft_id: str,
    payload: EmailDraftUpdateRequest,
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    """Apply the employee's edits; the approval and sensitive-data checks re-run."""
    return _run(lambda: update_draft(
        db, draft_id, actor,
        subject=payload.subject,
        body=payload.body,
        recipient_emails=payload.recipient_emails,
        add_groups=payload.add_groups,
    ))


@router.post("/{draft_id}/submit", response_model=EmailDraftResponse)
def submit_email_draft(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    return _run(lambda: submit_for_approval(db, draft_id, actor))


@router.post("/{draft_id}/decision", response_model=EmailDraftResponse)
def decide_email_draft(
    draft_id: str,
    payload: EmailDraftDecisionRequest,
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    return _run(lambda: decide_draft(db, draft_id, actor, decision=payload.decision, note=payload.note))


@router.post("/{draft_id}/send", response_model=EmailDraftResponse)
def send_email_draft(
    draft_id: str,
    payload: EmailDraftSendRequest,
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    """Send the previewed e-mail; requires ``confirm`` from the employee."""
    return _run(lambda: send_draft(
        db, draft_id, actor,
        confirm=payload.confirm,
        acknowledge_sensitive=payload.acknowledge_sensitive,
    ))


@router.delete("/{draft_id}", response_model=EmailDraftResponse)
def discard_email_draft(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    return _run(lambda: discard_draft(db, draft_id, actor))
