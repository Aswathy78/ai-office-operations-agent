"""HTTP API for assistant tool selection and execution (Story 6.5).

  GET  /api/v1/assistant/tools                        tools and whether the caller may use them
  POST /api/v1/assistant/tools/{tool_name}/execute    validate and run one tool call
  GET  /api/v1/assistant/tools/executions             the caller's recent tool executions
  GET  /api/v1/assistant/tools/executions/{id}        one execution of the caller

Write tools need an idempotency key (body ``idempotency_key`` or the
``Idempotency-Key`` header). Repeating a call with the same key and arguments
returns the first result with ``replayed: true``; reusing the key for a different
call is a 409.

Story 6.6: every write passes the permission, conflict and risk checks. A
denied write is 403 and a conflicting one 409, each with the reasons. Low-risk
writes run (200); medium/high-risk writes return 202 with
``status: "approval_required"`` and the pending approval — nothing runs until it
is approved through ``/api/v1/assistant/approvals``. ``confirm`` is accepted for
backward compatibility but no longer skips any check.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.assistant_tools import (
    ToolArgumentError,
    ToolConflictBlocked,
    ToolError,
    ToolPermissionDenied,
    execute_tool,
    get_registry,
    scopes_for_role,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import AssistantConversation, AssistantToolExecution

router = APIRouter(prefix="/api/v1/assistant/tools", tags=["AI Assistant Tools"])


class ToolExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
    # Ignored since Story 6.6: approval comes from /api/v1/assistant/approvals.
    confirm: bool = False
    conversation_id: str | None = None


class ToolExecutionResponse(BaseModel):
    execution_id: str | None
    tool_name: str
    status: str
    result: dict | None
    replayed: bool
    attempts: int
    duration_ms: int | None
    idempotency_key: str | None
    risk_level: str | None = None
    risk_reasons: list[str] = []
    approval: dict | None = None


class ToolExecutionRecord(BaseModel):
    id: str
    tool_name: str
    status: str
    risk_tier: str
    idempotency_key: str | None
    conversation_id: str | None
    arguments: dict
    result: dict | None
    error: str | None
    attempts: int
    duration_ms: int | None
    created_at: datetime
    completed_at: datetime | None


def _record(row: AssistantToolExecution) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        id=row.id,
        tool_name=row.tool_name,
        status=row.status,
        risk_tier=row.risk_tier,
        idempotency_key=row.idempotency_key,
        conversation_id=row.conversation_id,
        arguments=row.arguments or {},
        result=row.result,
        error=row.error,
        attempts=row.attempts,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


@router.get("")
def list_tools(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
):
    """Every tool with its schema, scopes, risk tier, timeout and retry policy."""
    scopes = scopes_for_role(principal.role)
    return {
        "role": principal.role,
        "scopes": sorted(scopes),
        "tools": [spec.describe(scopes) for spec in get_registry().all()],
    }


@router.post("/{tool_name}/execute", response_model=ToolExecutionResponse)
def execute_tool_endpoint(
    tool_name: str,
    req: ToolExecuteRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = req.idempotency_key or idempotency_key_header
    if req.idempotency_key and idempotency_key_header and req.idempotency_key != idempotency_key_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The body and header idempotency keys differ.",
        )
    if req.conversation_id:
        conversation = db.get(AssistantConversation, req.conversation_id)
        if conversation is None or conversation.user_id != principal.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found.",
            )
    try:
        outcome = execute_tool(
            db,
            principal,
            tool_name,
            req.arguments,
            idempotency_key=key,
            conversation_id=req.conversation_id,
        )
    except ToolArgumentError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, "errors": exc.errors},
        ) from exc
    except ToolPermissionDenied as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, "reasons": exc.reasons},
        ) from exc
    except ToolConflictBlocked as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, "conflicts": exc.conflicts},
        ) from exc
    except ToolError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if outcome.status == "approval_required":
        response.status_code = status.HTTP_202_ACCEPTED
    return ToolExecutionResponse(**outcome.as_dict())


@router.get("/executions", response_model=list[ToolExecutionRecord])
def list_executions(
    limit: int = Query(default=20, ge=1, le=100),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(AssistantToolExecution)
        .filter(AssistantToolExecution.actor_id == principal.user_id)
        .order_by(AssistantToolExecution.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_record(row) for row in rows]


@router.get("/executions/{execution_id}", response_model=ToolExecutionRecord)
def get_execution(
    execution_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    row = db.get(AssistantToolExecution, execution_id)
    if row is None or row.actor_id != principal.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found.")
    return _record(row)
