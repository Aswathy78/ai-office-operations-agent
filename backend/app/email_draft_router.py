"""E-mail drafting from chat: preview, edit, approval and send (Story 4.3 / FR-20),
with the delivery lifecycle Draft / Approval / Queued / Sending / Sent / Failed / Retry
(Story 4.4 / FR-22)."""

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
    cancel_send,
    create_draft_from_chat,
    decide_draft,
    discard_draft,
    get_draft,
    list_approval_queue,
    list_delivery_issues,
    list_directory_groups,
    list_draft_events,
    list_my_drafts,
    retry_draft,
    send_draft,
    submit_for_approval,
    update_draft,
)
from app.email_delivery_service import InvalidEmailTransition, max_attempts
from app.meeting.models import EmailDraft, EmailDraftEvent
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


EmailLifecycleStatus = Literal["draft", "approval", "queued", "sending", "sent", "failed", "retry"]


class DeliveryResult(BaseModel):
    email: str
    # "sent" only once the mail provider accepted the message for this recipient.
    status: Literal["sent", "failed"]
    error: str | None = None
    message_id: str | None = None
    accepted_at: datetime | None = None
    attempt: int | None = None


class EmailDraftEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    from_status: EmailLifecycleStatus | None
    to_status: EmailLifecycleStatus
    actor_id: str
    detail: str | None
    created_at: datetime


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
    status: EmailLifecycleStatus
    status_changed_at: datetime | None
    approval_decision: Literal["approved", "rejected"] | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    delivery_results: list[DeliveryResult]
    sent_at: datetime | None
    queued_at: datetime | None
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    acknowledged_sensitive: bool
    discarded_at: datetime | None
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
        max_attempts=max_attempts(),
    )


def _run(action: Callable[[], EmailDraft]) -> EmailDraftResponse:
    try:
        return _response(action())
    except EmailDraftNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except EmailDraftDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except (EmailDraftConflict, InvalidEmailTransition) as exc:
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
    request_status: EmailLifecycleStatus | Literal[""] | None = Query(default="approval", alias="status"),
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    try:
        drafts = list_approval_queue(db, actor, request_status or None)
    except EmailDraftDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return [_response(draft) for draft in drafts]


@router.get("/delivery-issues", response_model=list[EmailDraftResponse])
def get_email_delivery_issues(actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    """Failed and Retry e-mails across the organisation (manager dashboard)."""
    try:
        drafts = list_delivery_issues(db, actor)
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
    """Queue the previewed e-mail for delivery; requires ``confirm`` from the employee."""
    return _run(lambda: send_draft(
        db, draft_id, actor,
        confirm=payload.confirm,
        acknowledge_sensitive=payload.acknowledge_sensitive,
    ))


@router.delete("/{draft_id}", response_model=EmailDraftResponse)
def discard_email_draft(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    return _run(lambda: discard_draft(db, draft_id, actor))


@router.post("/{draft_id}/retry", response_model=EmailDraftResponse)
def retry_email_draft(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    """Failed -> Retry: deliver again to recipients the provider has not accepted."""
    return _run(lambda: retry_draft(db, draft_id, actor))


@router.post("/{draft_id}/cancel", response_model=EmailDraftResponse)
def cancel_email_draft(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    """Queued -> Draft, if delivery has not started yet."""
    return _run(lambda: cancel_send(db, draft_id, actor))


@router.get("/{draft_id}/events", response_model=list[EmailDraftEventResponse])
def get_email_draft_events(draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db)):
    """Every status change of the e-mail, oldest first."""
    try:
        events: list[EmailDraftEvent] = list_draft_events(db, draft_id, actor)
    except EmailDraftNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except EmailDraftDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return [EmailDraftEventResponse.model_validate(event) for event in events]
