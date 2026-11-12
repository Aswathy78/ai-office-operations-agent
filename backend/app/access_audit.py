"""Sanitized audit records for HTTP authorization denials."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from fastapi import HTTPException, Request, WebSocket, status
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import AuthAuditEvent


def record_access_denial(
    db: Session,
    request: Request | WebSocket,
    *,
    status_code: int,
    principal: AuthenticatedPrincipal | None = None,
    resource: str | None = None,
) -> None:
    """Persist a denial without recording request values, credentials, or payloads."""
    is_websocket = request.scope.get("type") == "websocket"
    method = str(request.scope.get("method", "UNKNOWN")).upper()
    if not method.isalpha():
        method = "UNKNOWN"
    detail = (
        f"WEBSOCKET {status_code} denied."
        if is_websocket
        else f"HTTP {status_code} {method[:10]} denied."
    )
    if resource:
        detail = f"{detail[:-1]} for {resource}."
    db.add(
        AuthAuditEvent(
            id=str(uuid.uuid4()),
            event="access_denied",
            user_id=principal.user_id if principal else None,
            actor_id=principal.user_id if principal else None,
            session_id=principal.session_id if principal else None,
            detail=detail,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def deny_access(
    db: Session,
    request: Request,
    *,
    principal: AuthenticatedPrincipal | None,
    detail: str,
    status_code: int = status.HTTP_403_FORBIDDEN,
    resource: str | None = None,
) -> None:
    record_access_denial(
        db,
        request,
        status_code=status_code,
        principal=principal,
        resource=resource,
    )
    raise HTTPException(status_code=status_code, detail=detail)
