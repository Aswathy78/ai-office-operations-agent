"""E-mail drafting from chat: preview, edit, approval and send (Story 4.3 / FR-20),
with the delivery lifecycle Draft / Approval / Queued / Sending / Sent / Failed / Retry
(Story 4.4 / FR-22)."""

import re
from typing import Callable, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.plain_errors import PlainError
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.authentication import get_authenticated_principal, require_authenticated_principal
from app.access_audit import deny_access
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
    extract_topic,
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
import hmac
import os

from app.email_delivery_service import InvalidEmailTransition, max_attempts
from app.email_escalation_service import (
    SubscriptionConfirmation,
    list_notifications,
    parse_bounce_payload,
    record_bounce,
)
from app.meeting.models import EmailDraft, EmailDraftEvent, EmailFailureNotification
from app.time_utils import UtcDateTime

router = APIRouter(prefix="/api/v1/email-drafts", tags=["E-mail drafts"])

_EMAIL_DOCUMENT_CONTEXT_INTENT = re.compile(
    r"\b(?:handbook|policies?|SOPs?|company documents?|uploaded documents?|"
    r"according to|based on|casual leave|maternity leave|paternity leave|"
    r"working hours|core hours|leave entitlement|HR benefits)\b",
    re.IGNORECASE,
)


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
    # "sent" only once the mail provider accepted the message for this recipient;
    # "bounced" when a hard bounce was reported for it afterwards.
    status: Literal["sent", "failed", "bounced"]
    error: PlainError = None
    message_id: str | None = None
    accepted_at: UtcDateTime | None = None
    attempt: int | None = None
    provider: str | None = None
    fallback_used: bool = False


class BounceRecord(BaseModel):
    recipient: str
    type: Literal["hard", "soft"]
    source: str
    code: int | None = None
    diagnostic: str | None = None
    message_id: str | None = None
    recorded_at: UtcDateTime


class FailureNotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    failure_event_sequence: int
    audience: Literal["requester", "admin"]
    channel: Literal["email", "in_app"]
    recipient_email: str | None
    status: Literal["pending", "sent", "failed", "recorded"]
    attempts: int
    next_attempt_at: UtcDateTime | None
    provider: str | None
    error: PlainError
    sent_at: UtcDateTime | None
    created_at: UtcDateTime


class BounceReportResponse(BaseModel):
    recorded: int
    unmatched: list[str]
    draft_ids: list[str]


class EmailDraftEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    from_status: EmailLifecycleStatus | None
    to_status: EmailLifecycleStatus
    actor_id: str
    detail: str | None
    created_at: UtcDateTime


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
    status_changed_at: UtcDateTime | None
    approval_decision: Literal["approved", "rejected"] | None
    decided_by: str | None
    decided_at: UtcDateTime | None
    decision_note: str | None
    delivery_results: list[DeliveryResult]
    sent_at: UtcDateTime | None
    queued_at: UtcDateTime | None
    attempt_count: int
    max_attempts: int
    next_attempt_at: UtcDateTime | None
    last_error: PlainError
    acknowledged_sensitive: bool
    discarded_at: UtcDateTime | None
    bounces: list[BounceRecord]
    escalated_at: UtcDateTime | None
    notifications: list[FailureNotificationResponse]
    created_at: UtcDateTime
    updated_at: UtcDateTime


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
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> EmailActor:
    return EmailActor(id=principal.user_id, role=principal.role, team=principal.team)


def _response(draft: EmailDraft) -> EmailDraftResponse:
    data = {column.name: getattr(draft, column.name) for column in EmailDraft.__table__.columns}
    return EmailDraftResponse(
        **data,
        recipient_count=len(draft.recipients),
        approval_threshold=approval_recipient_threshold(),
        max_attempts=max_attempts(),
        notifications=[
            FailureNotificationResponse.model_validate(n) for n in draft.failure_notifications
        ],
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
def get_directory_groups(
    _principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    return list_directory_groups(db)


@router.post("", response_model=EmailDraftResponse, status_code=status.HTTP_201_CREATED)
def create_email_draft(
    payload: EmailDraftCreateRequest,
    actor: EmailActor = Depends(_actor),
    db: Session = Depends(get_db),
):
    """Draft an e-mail from a chat message. Nothing is sent."""
    retrieved_context = None
    source_document_ids: list[str] = []
    from app import assistant_planner

    if (
        assistant_planner.needs_retrieval(payload.prompt)
        and _EMAIL_DOCUMENT_CONTEXT_INTENT.search(payload.prompt)
    ):
        search_query = extract_topic(payload.prompt)
        if re.search(r"\bcore\s+hours?\b", search_query, re.IGNORECASE):
            search_query = (
                "employee working hours "
                + re.sub(
                    r"\b(?:the\s+)?company['’]s\s*",
                    "",
                    search_query,
                    flags=re.IGNORECASE,
                )
            )
        results = assistant_planner.retrieve_semantic_document_context(
            db,
            actor_id=actor.id,
            query=search_query,
        )
        # Story 7.4: only passages the sender may read right now go into the draft.
        results = assistant_planner.authorize_document_context(
            db, actor_id=actor.id, context=results
        )
        source_document_ids = [
            item["document_id"] for item in results if item.get("document_id")
        ]
        retrieved_context = "\n\n".join(
            f"[{item['source']}]\n{item['snippet']}"
            for item in results
            if item.get("snippet")
        )
        if not retrieved_context:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "I couldn't find relevant information in the indexed company documents, "
                    "so I didn't create a draft with unverified company details."
                ),
            )

    return _run(lambda: create_draft_from_chat(
        db, actor,
        prompt=payload.prompt,
        sender_name=payload.sender_name,
        sender_email=payload.sender_email,
        mode=payload.mode,
        retrieved_context=retrieved_context,
        source_document_ids=source_document_ids,
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


@router.post("/bounces", response_model=BounceReportResponse)
def report_email_bounce(
    request: Request,
    payload: dict = Body(...),
    x_bounce_webhook_token: str | None = Header(default=None),
    principal: AuthenticatedPrincipal | None = Depends(get_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Record bounce reports (generic JSON, or Amazon SES notifications via SNS).

    Called by the mail provider's webhook with ``X-Bounce-Webhook-Token`` set to
    ``EMAIL_BOUNCE_WEBHOOK_TOKEN``, or by an admin.
    """
    expected = os.getenv("EMAIL_BOUNCE_WEBHOOK_TOKEN", "")
    token_ok = bool(expected) and bool(x_bounce_webhook_token) and hmac.compare_digest(
        expected, x_bounce_webhook_token
    )
    if not token_ok and (principal is None or principal.role != "admin"):
        deny_access(
            db, request, principal=principal,
            detail="A valid bounce webhook token or authenticated administrator is required.",
        )
    try:
        reports = parse_bounce_payload(payload)
    except SubscriptionConfirmation as confirmation:
        # Never auto-confirm: an admin confirms the subscription in the AWS console.
        raise HTTPException(
            status.HTTP_202_ACCEPTED,
            f"SNS subscription confirmation received; confirm it at {confirmation.subscribe_url}",
        ) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    recorded, unmatched, draft_ids = 0, [], []
    for report in reports:
        try:
            draft = record_bounce(db, **report) if report.get("message_id") else None
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        if draft is None:
            unmatched.append(report.get("message_id") or "(no message id)")
        else:
            recorded += 1
            if draft.id not in draft_ids:
                draft_ids.append(draft.id)
    return BounceReportResponse(recorded=recorded, unmatched=unmatched, draft_ids=draft_ids)


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


@router.get("/{draft_id}/notifications", response_model=list[FailureNotificationResponse])
def get_email_failure_notifications(
    draft_id: str, actor: EmailActor = Depends(_actor), db: Session = Depends(get_db),
):
    """Who was told that this e-mail failed, and whether that message got through."""
    try:
        get_draft(db, draft_id, actor)
    except EmailDraftNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except EmailDraftDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    notifications: list[EmailFailureNotification] = list_notifications(db, draft_id)
    return [FailureNotificationResponse.model_validate(n) for n in notifications]
