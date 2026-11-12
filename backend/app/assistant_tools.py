"""Tool registry and executor for the AI assistant (Story 6.5 — Select tools & execute).

Every tool the agent can call is a ``ToolSpec`` that declares:

* ``input_schema``   JSON schema of its arguments. Calls that do not validate are
                     rejected before anything runs.
* ``required_scopes`` permissions the caller's role must grant (``ROLE_SCOPES``).
* ``risk_tier``      "read", "low", "medium" or "high". Anything but "read" is a
                     write and must carry an idempotency key; "medium" and "high"
                     also need explicit confirmation.
* ``timeout_seconds`` per-attempt time limit.
* ``retry_policy``   how many attempts, the back-off between them, and which
                     errors are worth retrying.

``execute_tool`` is the only way a tool runs. It checks scopes, validates the
arguments, enforces idempotency through the ``assistant_tool_executions`` ledger,
runs the handler with the timeout and retry policy, and records the outcome.
Handlers delegate to the existing application services; they never just describe
what would happen.

Story 6.6 — before every write ``execute_tool`` also runs the write guard:

1. Permission: scopes, then the tool's resource rules (``authorize``), e.g. a
   manager may only assign tasks inside their own team.
2. Conflicts: the tool's ``detect_conflicts`` (busy calendar slot, duplicate task
   or e-mail, workflow already running) plus an identical request already
   waiting for approval.
3. Risk: Low, Medium or High from the tool's baseline and ``assess_risk``.

Low-risk writes run at once. Medium-risk writes pause as "Approval Required"
until the requester confirms; high-risk writes until someone else with approval
rights approves (``app.assistant_approvals``). Approved actions run through this
same function again — every check is repeated — with the requester's own
principal. Every decision and outcome is written to ``assistant_audit_events``.

Story 6.7 verifies task, meeting and email writes from persisted records after
execution. A mismatch retries and re-plans only the read-back, then escalates
to the requester and their manager; it never repeats the external write.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import time
from typing import Any, Literal
import uuid

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import (
    ACTION_APPROVAL_STATUS_EXECUTED,
    ACTION_APPROVAL_STATUS_EXPIRED,
    ACTION_APPROVAL_STATUS_FAILED,
    ACTION_APPROVAL_STATUS_PENDING,
    ACTION_APPROVAL_STATUS_EXECUTING,
    AssistantActionApproval,
    AssistantAuditEvent,
    AssistantToolExecution,
    TOOL_EXECUTION_STATUS_FAILED,
    TOOL_EXECUTION_STATUS_RUNNING,
    TOOL_EXECUTION_STATUS_SUCCEEDED,
    TOOL_EXECUTION_STATUS_TIMED_OUT,
)

logger = logging.getLogger(__name__)

RiskTier = Literal["read", "low", "medium", "high"]
RISK_TIERS: tuple[str, ...] = ("read", "low", "medium", "high")
CONFIRMATION_RISK_TIERS = frozenset({"medium", "high"})
RiskLevel = Literal["low", "medium", "high"]
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
OUTCOME_APPROVAL_REQUIRED = "approval_required"


def max_risk(*levels: str) -> str:
    return max(levels, key=lambda level: _RISK_ORDER[level])

TOOL_CATEGORIES = (
    "meeting",
    "email",
    "calendar",
    "task",
    "document",
    "notification",
    "employee_directory",
    "database",
    "workflow",
)

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{8,200}$")

# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

EMPLOYEE_SCOPES = frozenset({
    "directory.read",
    "calendar.read",
    "meetings.read",
    # Any employee can already schedule meetings and send e-mail by chat
    # (Stories 4.x / 5.1); the e-mail approval rules still apply.
    "meetings.write",
    "email.compose",
    "email.send",
    "tasks.read",
    "documents.read",
})
# Managers (team leads) and operations staff can also act on other people's work.
MANAGER_SCOPES = EMPLOYEE_SCOPES | frozenset({
    "tasks.write",
    "notifications.send",
    "database.read",
})
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "employee": EMPLOYEE_SCOPES,
    "manager": MANAGER_SCOPES,
    "hr_operations": MANAGER_SCOPES | frozenset({"workflow.run"}),
    "admin": MANAGER_SCOPES | frozenset({"workflow.run"}),
}


def scopes_for_role(role: str) -> frozenset[str]:
    return ROLE_SCOPES.get(role, frozenset())


# ---------------------------------------------------------------------------
# Errors (each carries the HTTP status the API reports it with)
# ---------------------------------------------------------------------------


class ToolError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.execution_id: str | None = None


class UnknownToolError(ToolError):
    status_code = 404


class ToolPermissionDenied(ToolError):
    status_code = 403

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = reasons or [message]


class ToolNotFound(ToolError):
    """A record the call refers to does not exist."""

    status_code = 404


class ToolArgumentError(ToolError):
    """The arguments do not match the tool's declared schema (the call is rejected)."""

    status_code = 422

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


class ToolConfirmationRequired(ToolError):
    status_code = 428


class IdempotencyConflict(ToolError):
    """The key was already used for a different call, or that call is still running."""

    status_code = 409


class ToolExecutionError(ToolError):
    """The operation was attempted and failed for a business reason (not retried)."""

    status_code = 422


class ToolConflictError(ToolExecutionError):
    """The operation conflicts with the current state (e.g. a busy calendar slot)."""

    status_code = 409


class ToolConflictBlocked(ToolConflictError):
    """The write guard found conflicts before anything ran (Story 6.6)."""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.conflicts = conflicts


class ToolApprovalError(ToolError):
    """The approval cannot be used: closed, expired, mismatched or insufficient."""

    status_code = 409


class ToolTransientError(ToolError):
    """A temporary failure that the retry policy may retry."""

    status_code = 503


class ToolTimeoutError(ToolError):
    status_code = 504


# ---------------------------------------------------------------------------
# JSON schema validation
# ---------------------------------------------------------------------------
#
# The backend has no jsonschema dependency, so this validates the subset of
# JSON Schema the tool catalog uses. Unsupported keywords are refused when a
# tool registers, so a schema can never be silently under-enforced.

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "minItems", "maxItems", "uniqueItems", "anyOf",
    "description", "default", "title", "examples",
})
_EMAIL_FORMAT = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is an int in Python but never a JSON integer/number.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def check_schema_definition(schema: dict, path: str = "$") -> None:
    """Raise ValueError when a tool schema uses keywords this validator cannot enforce."""
    if not isinstance(schema, dict):
        raise ValueError(f"Schema at {path} must be an object.")
    unknown = set(schema) - SUPPORTED_SCHEMA_KEYWORDS
    if unknown:
        raise ValueError(f"Unsupported schema keyword(s) at {path}: {sorted(unknown)}")
    types = schema.get("type")
    for type_name in [types] if isinstance(types, str) else types or []:
        if type_name not in _TYPE_CHECKS:
            raise ValueError(f"Unsupported type {type_name!r} at {path}.")
    for name, child in (schema.get("properties") or {}).items():
        check_schema_definition(child, f"{path}.{name}")
    if isinstance(schema.get("additionalProperties"), dict):
        check_schema_definition(schema["additionalProperties"], f"{path}.*")
    if "items" in schema:
        check_schema_definition(schema["items"], f"{path}[]")
    for index, option in enumerate(schema.get("anyOf") or []):
        check_schema_definition(option, f"{path}.anyOf[{index}]")


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A bare date is not a date-time; a date-time must say which time zone it is in.
    if "T" not in value.upper() or parsed.tzinfo is None:
        return None
    return parsed


def _format_error(value: str, fmt: str) -> str | None:
    if fmt == "email":
        return None if _EMAIL_FORMAT.fullmatch(value) else "must be a valid e-mail address"
    if fmt == "date-time":
        return None if _parse_datetime(value) else (
            "must be an ISO-8601 date-time with a time zone, e.g. 2026-10-23T10:00:00+00:00"
        )
    if fmt == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return "must be an ISO-8601 date, e.g. 2026-10-23"
        return None
    if fmt == "uuid":
        try:
            uuid.UUID(value)
        except ValueError:
            return "must be a UUID"
        return None
    return None


def validate_against_schema(value: Any, schema: dict, path: str = "$") -> list[dict]:
    """Return a list of ``{"path", "message"}`` errors; empty when ``value`` is valid."""
    errors: list[dict] = []

    def fail(message: str, at: str = path) -> None:
        errors.append({"path": at, "message": message})

    expected = schema.get("type")
    if expected is not None:
        options = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS[name](value) for name in options):
            fail(f"must be of type {' or '.join(options)}")
            return errors

    if "const" in schema and value != schema["const"]:
        fail(f"must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        fail("must be one of: " + ", ".join(repr(option) for option in schema["enum"]))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            fail(f"must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            fail(f"must be at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            fail(f"must match pattern {schema['pattern']}")
        if "format" in schema:
            message = _format_error(value, schema["format"])
            if message:
                fail(message)

    if _TYPE_CHECKS["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            fail(f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            fail(f"must be <= {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            fail(f"must contain at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            fail(f"must contain at most {schema['maxItems']} item(s)")
        if schema.get("uniqueItems"):
            seen: list[str] = []
            for item in value:
                key = json.dumps(item, sort_keys=True, default=str)
                if key in seen:
                    fail("must not contain duplicate items")
                    break
                seen.append(key)
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate_against_schema(item, schema["items"], f"{path}[{index}]"))

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                fail("is required", f"{path}.{name}")
        additional = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                errors.extend(validate_against_schema(item, properties[name], f"{path}.{name}"))
            elif additional is False:
                fail("is not an allowed property", f"{path}.{name}")
            elif isinstance(additional, dict):
                errors.extend(validate_against_schema(item, additional, f"{path}.{name}"))

    if "anyOf" in schema:
        if not any(not validate_against_schema(value, option, path) for option in schema["anyOf"]):
            descriptions = [
                option.get("description") or json.dumps(option, sort_keys=True)
                for option in schema["anyOf"]
            ]
            fail("must satisfy at least one of: " + "; ".join(descriptions))

    return errors


def parse_datetime_argument(value: str) -> datetime:
    """Parse a schema-validated ``date-time`` argument as an aware UTC datetime."""
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ToolArgumentError(f"{value!r} is not a valid date-time.")
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Tool specification and registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    # Seconds to wait before attempt 2, 3, ...; the last value repeats.
    backoff_seconds: tuple[float, ...] = ()
    retry_on: tuple[type[BaseException], ...] = (ToolTransientError,)

    def delay_before(self, attempt: int) -> float:
        if not self.backoff_seconds:
            return 0.0
        return self.backoff_seconds[min(attempt - 2, len(self.backoff_seconds) - 1)]

    def as_dict(self) -> dict:
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": list(self.backoff_seconds),
            "retry_on": [error.__name__ for error in self.retry_on],
        }


NO_RETRY = RetryPolicy(max_attempts=1, retry_on=())
# Reads are safe to repeat; retry a locked SQLite database or a transient failure.
READ_RETRY = RetryPolicy(
    max_attempts=3,
    backoff_seconds=(0.2, 0.5),
    retry_on=(ToolTransientError, OperationalError),
)


@dataclass
class ToolContext:
    """What a handler receives besides its validated arguments."""

    db: Session
    principal: AuthenticatedPrincipal
    scopes: frozenset[str]
    execution_id: str
    idempotency_key: str | None
    conversation_id: str | None
    attempt: int
    deadline: float
    # Progress saved by an earlier attempt of the same idempotent call.
    progress: dict = field(default_factory=dict)
    # Story 6.6: who approved a high-risk call ({id, role, team, approval_id}).
    # The handler still acts as ``principal``; this only lets it record the decision.
    approved_by: dict | None = None

    def save_progress(self, **values: Any) -> None:
        """Durably record partial progress so a re-run resumes instead of duplicating."""
        self.progress.update(_json_safe(values))
        row = self.db.get(AssistantToolExecution, self.execution_id)
        if row is not None:
            row.progress = dict(self.progress)
            self.db.commit()

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise ToolTimeoutError("The tool ran past its time limit.")


ToolHandler = Callable[[ToolContext, dict], dict]
ToolValidator = Callable[[dict], list[dict]]
# Story 6.6 write-guard hooks; each receives (db, principal, arguments).
ToolAuthorizer = Callable[[Session, AuthenticatedPrincipal, dict], list[str]]
ToolConflictDetector = Callable[[Session, AuthenticatedPrincipal, dict], list[dict]]
ToolRiskAssessor = Callable[[Session, AuthenticatedPrincipal, dict], tuple[str, list[str]]]
ToolDescriber = Callable[[dict], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    description: str
    input_schema: dict
    required_scopes: frozenset[str]
    # "read", or the baseline risk of a write before assess_risk adds factors.
    risk_tier: RiskTier
    timeout_seconds: float
    retry_policy: RetryPolicy
    handler: ToolHandler
    # Cross-field rules a JSON schema cannot express (e.g. end after start).
    validator: ToolValidator | None = None
    # Resource-level permission rules; returns the reasons the call is denied.
    authorize: ToolAuthorizer | None = None
    # Returns ``{"code", "message"}`` conflicts that block the write.
    detect_conflicts: ToolConflictDetector | None = None
    # Returns (risk level, reasons); the effective risk is never below risk_tier.
    assess_risk: ToolRiskAssessor | None = None
    # One-line description of a call for approval requests and the audit log.
    describe_call: ToolDescriber | None = None

    @property
    def is_write(self) -> bool:
        return self.risk_tier != "read"

    @property
    def requires_idempotency_key(self) -> bool:
        return self.is_write

    @property
    def requires_confirmation(self) -> bool:
        """True when even the baseline risk needs an approval before running."""
        return self.risk_tier in CONFIRMATION_RISK_TIERS

    def summarize_call(self, arguments: dict) -> str:
        if self.describe_call is not None:
            try:
                return self.describe_call(arguments)
            except (KeyError, TypeError, ValueError):
                pass
        return f"{self.name} {json.dumps(arguments, sort_keys=True, default=str)[:300]}"

    def describe(self, scopes: frozenset[str] | None = None) -> dict:
        data = {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "input_schema": self.input_schema,
            "required_scopes": sorted(self.required_scopes),
            "risk_tier": self.risk_tier,
            "baseline_risk": None if self.risk_tier == "read" else self.risk_tier,
            "is_write": self.is_write,
            "requires_idempotency_key": self.requires_idempotency_key,
            "requires_confirmation": self.requires_confirmation,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": self.retry_policy.as_dict(),
        }
        if scopes is not None:
            data["permitted"] = self.required_scopes <= scopes
        return data


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Add a tool after checking it declares everything Story 6.5 requires."""
        if not re.fullmatch(r"[a-z_]+\.[a-z_]+", spec.name):
            raise ValueError(f"Tool name {spec.name!r} must look like 'category.action'.")
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} is already registered.")
        if spec.category not in TOOL_CATEGORIES:
            raise ValueError(f"Tool {spec.name!r} has unknown category {spec.category!r}.")
        if not spec.required_scopes:
            raise ValueError(f"Tool {spec.name!r} must declare its required scopes.")
        known_scopes = frozenset().union(*ROLE_SCOPES.values())
        if not spec.required_scopes <= known_scopes:
            raise ValueError(
                f"Tool {spec.name!r} requires undefined scope(s): "
                f"{sorted(spec.required_scopes - known_scopes)}"
            )
        if spec.risk_tier not in RISK_TIERS:
            raise ValueError(f"Tool {spec.name!r} has invalid risk tier {spec.risk_tier!r}.")
        if not 0 < spec.timeout_seconds <= 300:
            raise ValueError(f"Tool {spec.name!r} needs a timeout between 0 and 300 seconds.")
        if spec.retry_policy.max_attempts < 1:
            raise ValueError(f"Tool {spec.name!r} must allow at least one attempt.")
        if spec.input_schema.get("type") != "object" or spec.input_schema.get(
            "additionalProperties"
        ) is not False:
            raise ValueError(
                f"Tool {spec.name!r} input schema must be an object that forbids unknown properties."
            )
        check_schema_definition(spec.input_schema)
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownToolError(f"Unknown tool {name!r}.")
        return spec

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda spec: spec.name)


_REGISTRY: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Return the tool registry, loading the built-in catalog on first use."""
    global _REGISTRY
    if _REGISTRY is None:
        registry = ToolRegistry()
        from app.assistant_tool_catalog import register_builtin_tools

        register_builtin_tools(registry)
        _REGISTRY = registry
    return _REGISTRY


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

# Handlers run on worker threads so a slow provider cannot exceed its timeout.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="assistant-tool")
# Indirection so tests can skip real back-off waits.
_sleep = time.sleep


@dataclass
class ToolExecutionOutcome:
    execution_id: str | None
    tool_name: str
    # "succeeded", or "approval_required" when the write was paused (Story 6.6).
    status: str
    result: dict | None
    replayed: bool
    attempts: int
    duration_ms: int | None
    idempotency_key: str | None
    risk_level: str | None = None
    risk_reasons: list[str] = field(default_factory=list)
    approval: dict | None = None

    def as_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "result": self.result,
            "replayed": self.replayed,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "idempotency_key": self.idempotency_key,
            "risk_level": self.risk_level,
            "risk_reasons": self.risk_reasons,
            "approval": self.approval,
        }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def arguments_hash(arguments: dict) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_tool_call(
    principal: AuthenticatedPrincipal,
    tool_name: str,
    arguments: Any,
    *,
    idempotency_key: str | None = None,
    require_key: bool = True,
) -> ToolSpec:
    """Check scopes, arguments and the idempotency key without running anything."""
    if principal.role not in ROLE_SCOPES:
        raise ToolPermissionDenied(
            "The authenticated account has an unrecognized role; tool access is denied."
        )
    spec = get_registry().get(tool_name)
    missing = spec.required_scopes - scopes_for_role(principal.role)
    if missing:
        raise ToolPermissionDenied(
            f"The tool {tool_name!r} requires the scope(s) {', '.join(sorted(missing))}, "
            f"which the role '{principal.role}' does not have."
        )
    if not isinstance(arguments, dict):
        raise ToolArgumentError(
            "Tool arguments must be a JSON object.",
            [{"path": "$", "message": "must be of type object"}],
        )
    errors = validate_against_schema(arguments, spec.input_schema)
    if not errors and spec.validator is not None:
        errors = spec.validator(arguments)
    if errors:
        summary = "; ".join(f"{error['path']} {error['message']}" for error in errors[:5])
        raise ToolArgumentError(
            f"Invalid arguments for {tool_name!r}: {summary}",
            errors,
        )
    if spec.requires_idempotency_key and require_key:
        if not idempotency_key:
            raise ToolArgumentError(
                f"The write tool {tool_name!r} requires an idempotency key.",
                [{"path": "idempotency_key", "message": "is required for write operations"}],
            )
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
            raise ToolArgumentError(
                "The idempotency key must be 8-200 characters of letters, digits, '.', '_', ':' or '-'.",
                [{"path": "idempotency_key", "message": "has an invalid format"}],
            )
    return spec


def _outcome(row: AssistantToolExecution, replayed: bool) -> ToolExecutionOutcome:
    return ToolExecutionOutcome(
        execution_id=row.id,
        tool_name=row.tool_name,
        status=row.status,
        result=row.result,
        replayed=replayed,
        attempts=row.attempts,
        duration_ms=row.duration_ms,
        idempotency_key=row.idempotency_key,
    )


def _claim_ledger_row(
    db: Session,
    spec: ToolSpec,
    principal: AuthenticatedPrincipal,
    arguments: dict,
    digest: str,
    idempotency_key: str | None,
    conversation_id: str | None,
) -> tuple[AssistantToolExecution, bool]:
    """Return ``(row, replay)``; ``replay`` means the stored result must be returned."""
    if idempotency_key is not None:
        existing = (
            db.query(AssistantToolExecution)
            .filter(
                AssistantToolExecution.actor_id == principal.user_id,
                AssistantToolExecution.idempotency_key == idempotency_key,
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.tool_name != spec.name or existing.arguments_hash != digest:
                raise IdempotencyConflict(
                    "This idempotency key was already used for a different operation. "
                    "Use a new key for a new operation."
                )
            if existing.status == TOOL_EXECUTION_STATUS_SUCCEEDED:
                return existing, True
            if existing.status == TOOL_EXECUTION_STATUS_RUNNING:
                raise IdempotencyConflict(
                    "This operation is already running; wait for it to finish."
                )
            # Failed or timed out: run again, resuming from any saved progress.
            existing.status = TOOL_EXECUTION_STATUS_RUNNING
            existing.error = None
            existing.completed_at = None
            db.commit()
            return existing, False

    row = AssistantToolExecution(
        id=str(uuid.uuid4()),
        tool_name=spec.name,
        actor_id=principal.user_id,
        actor_role=principal.role,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
        risk_tier=spec.risk_tier,
        arguments=_json_safe(arguments),
        arguments_hash=digest,
        status=TOOL_EXECUTION_STATUS_RUNNING,
        attempts=0,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Another request claimed the same key first.
        db.rollback()
        return _claim_ledger_row(
            db, spec, principal, arguments, digest, idempotency_key, conversation_id
        )
    return row, False


def _run_attempt(
    spec: ToolSpec,
    engine,
    principal: AuthenticatedPrincipal,
    arguments: dict,
    execution_id: str,
    idempotency_key: str | None,
    conversation_id: str | None,
    attempt: int,
    progress: dict,
    approved_by: dict | None = None,
) -> dict:
    """One handler attempt in its own session (runs on a worker thread)."""
    with Session(bind=engine) as worker_db:
        context = ToolContext(
            db=worker_db,
            principal=principal,
            scopes=scopes_for_role(principal.role),
            execution_id=execution_id,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
            attempt=attempt,
            deadline=time.monotonic() + spec.timeout_seconds,
            progress=dict(progress),
            approved_by=approved_by,
        )
        try:
            result = spec.handler(context, arguments)
        except BaseException:
            worker_db.rollback()
            raise
        return _json_safe(result if result is not None else {})


def _finish_late(engine, execution_id: str, future) -> None:
    """Record the real outcome of an attempt that finished after its timeout."""
    if future.cancelled():
        return
    error = future.exception()
    with Session(bind=engine) as late_db:
        row = late_db.get(AssistantToolExecution, execution_id)
        if row is None or row.status != TOOL_EXECUTION_STATUS_TIMED_OUT:
            return
        if error is None:
            row.status = TOOL_EXECUTION_STATUS_SUCCEEDED
            row.result = future.result()
            row.error = None
        else:
            row.error = f"Finished after timing out: {error}"
        row.completed_at = datetime.now(timezone.utc)
        late_db.commit()


# ---------------------------------------------------------------------------
# Story 6.6: audit log, write guard and approval requests
# ---------------------------------------------------------------------------


def record_audit(
    db: Session,
    *,
    actor: AuthenticatedPrincipal | None = None,
    actor_id: str | None = None,
    actor_role: str | None = None,
    tool_name: str,
    event: str,
    detail: str,
    risk_level: str | None = None,
    approval_id: str | None = None,
    execution_id: str | None = None,
    conversation_id: str | None = None,
    data: dict | None = None,
) -> None:
    """Append one decision or outcome to ``assistant_audit_events`` (never raises)."""
    try:
        db.add(AssistantAuditEvent(
            id=str(uuid.uuid4()),
            actor_id=actor.user_id if actor else actor_id,
            actor_role=actor.role if actor else actor_role,
            tool_name=tool_name,
            event=event,
            risk_level=risk_level,
            approval_id=approval_id,
            execution_id=execution_id,
            conversation_id=conversation_id,
            detail=detail[:4000],
            data=_json_safe(data or {}),
        ))
        db.commit()
    except Exception:  # noqa: BLE001 - auditing must never break the request
        db.rollback()
        logger.exception("Could not write assistant audit event %s for %s", event, tool_name)


@dataclass
class GuardDecision:
    """Result of the write guard for one call."""

    risk_level: str
    risk_reasons: list[str]
    denials: list[str]
    conflicts: list[dict]

    @property
    def approver_kind(self) -> str | None:
        if self.risk_level == "high":
            return "approver"
        if self.risk_level == "medium":
            return "requester"
        return None


def evaluate_write(
    db: Session,
    spec: ToolSpec,
    principal: AuthenticatedPrincipal,
    arguments: dict,
    *,
    ignore_approval_id: str | None = None,
) -> GuardDecision:
    """Run the permission, conflict and risk checks for a write (no side effects)."""
    denials = list(spec.authorize(db, principal, arguments)) if spec.authorize else []
    conflicts: list[dict] = []
    if not denials:
        if spec.detect_conflicts:
            conflicts.extend(spec.detect_conflicts(db, principal, arguments))
        digest = arguments_hash(arguments)
        duplicate = (
            db.query(AssistantActionApproval)
            .filter(
                AssistantActionApproval.requester_id == principal.user_id,
                AssistantActionApproval.tool_name == spec.name,
                AssistantActionApproval.arguments_hash == digest,
                AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
                AssistantActionApproval.expires_at > datetime.now(timezone.utc),
            )
            .all()
        )
        for approval in duplicate:
            if approval.id != ignore_approval_id:
                conflicts.append({
                    "code": "pending_approval",
                    "message": (
                        "An identical request is already waiting for approval "
                        f"(request {approval.id})."
                    ),
                })
                break

    level, reasons = spec.risk_tier, [f"{spec.name} is a {spec.risk_tier}-risk action"]
    if spec.assess_risk is not None and not denials:
        assessed, factors = spec.assess_risk(db, principal, arguments)
        level = max_risk(level, assessed)
        reasons = reasons + list(factors)
    return GuardDecision(level, reasons, denials, conflicts)


def approval_ttl(risk_level: str) -> timedelta:
    """Medium: the requester's confirmation window; high: time for an approver to act."""
    if risk_level == "high":
        hours = _env_int("ASSISTANT_APPROVAL_TTL_HOURS", 72)
        return timedelta(hours=max(1, hours))
    return timedelta(minutes=max(1, _env_int("ASSISTANT_CONFIRMATION_TTL_MINUTES", 30)))


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def approval_as_dict(approval: AssistantActionApproval) -> dict:
    deadline = min(
        _as_aware(approval.expires_at),
        _as_aware(approval.created_at) + timedelta(hours=48),
    )
    return {
        "id": approval.id,
        "tool_name": approval.tool_name,
        "status": approval.status,
        "summary": approval.summary,
        "risk_level": approval.risk_level,
        "risk_reasons": approval.risk_reasons,
        "approver_kind": approval.approver_kind,
        "requester_id": approval.requester_id,
        "expires_at": deadline.isoformat(),
    }


def _request_approval(
    db: Session,
    spec: ToolSpec,
    principal: AuthenticatedPrincipal,
    arguments: dict,
    digest: str,
    idempotency_key: str,
    conversation_id: str | None,
    decision: GuardDecision,
    affected_people: list[dict] | None = None,
) -> AssistantActionApproval:
    """Get or create the pending approval for this call (one per requester and key)."""
    from app.assistant_approvals import expire_stale

    expire_stale(db)
    existing = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.requester_id == principal.user_id,
            AssistantActionApproval.idempotency_key == idempotency_key,
        )
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if existing is not None:
        if existing.tool_name != spec.name or existing.arguments_hash != digest:
            raise IdempotencyConflict(
                "This idempotency key was already used for a different operation. "
                "Use a new key for a new operation."
            )
        if existing.status == ACTION_APPROVAL_STATUS_PENDING and _as_aware(existing.expires_at) <= now:
            existing.status = ACTION_APPROVAL_STATUS_EXPIRED
            existing.updated_at = now
            db.commit()
            record_audit(db, actor=principal, tool_name=spec.name, event="expired",
                         risk_level=existing.risk_level, approval_id=existing.id,
                         detail="The approval request expired before anyone approved it.")
        if existing.status != ACTION_APPROVAL_STATUS_PENDING:
            note = f" Note: {existing.decision_note}" if existing.decision_note else ""
            raise ToolApprovalError(
                f"This request was already {existing.status}.{note} "
                "Send it again with a new idempotency key to start over."
            )
        from app.notification_service import notify_assistant_approval_request

        notify_assistant_approval_request(db, existing)
        return existing

    approval = AssistantActionApproval(
        id=str(uuid.uuid4()),
        tool_name=spec.name,
        requester_id=principal.user_id,
        requester_role=principal.role,
        requester_team=principal.team,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
        arguments=_json_safe(arguments),
        affected_people=_json_safe(affected_people) if affected_people is not None else None,
        arguments_hash=digest,
        summary=spec.summarize_call(arguments),
        risk_level=decision.risk_level,
        risk_reasons=decision.risk_reasons,
        approver_kind=decision.approver_kind,
        status=ACTION_APPROVAL_STATUS_PENDING,
        expires_at=now + approval_ttl(decision.risk_level),
        created_at=now,
        updated_at=now,
    )
    db.add(approval)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _request_approval(
            db, spec, principal, arguments, digest, idempotency_key, conversation_id, decision
        )
    from app.notification_service import notify_assistant_approval_request

    notify_assistant_approval_request(db, approval)
    who = "your confirmation" if approval.approver_kind == "requester" else (
        "approval by an admin or a specifically assigned Manager / HR user"
    )
    record_audit(
        db, actor=principal, tool_name=spec.name, event="approval_requested",
        risk_level=approval.risk_level, approval_id=approval.id,
        conversation_id=conversation_id,
        detail=f"{approval.summary} — {approval.risk_level} risk, waiting for {who}.",
        data={"risk_reasons": approval.risk_reasons},
    )
    return approval


def _check_approval(
    db: Session,
    spec: ToolSpec,
    principal: AuthenticatedPrincipal,
    digest: str,
    approval_id: str,
    decision: GuardDecision,
) -> AssistantActionApproval:
    """The approval must be decided, pending execution, and match this exact call."""
    approval = db.get(AssistantActionApproval, approval_id)
    if approval is None:
        raise ToolApprovalError("The approval request does not exist.")
    if (
        approval.requester_id != principal.user_id
        or approval.tool_name != spec.name
        or approval.arguments_hash != digest
    ):
        raise ToolApprovalError("The approval does not match this request.")
    if approval.status != ACTION_APPROVAL_STATUS_EXECUTING or approval.decided_by is None:
        raise ToolApprovalError(f"The approval is not usable (status {approval.status}).")
    if _as_aware(approval.expires_at) <= datetime.now(timezone.utc):
        raise ToolApprovalError("The approval expired; send the request again.")
    if _RISK_ORDER[decision.risk_level] > _RISK_ORDER[approval.risk_level]:
        raise ToolApprovalError(
            f"The risk rose from {approval.risk_level} to {decision.risk_level} since "
            "the request was made ("
            + "; ".join(decision.risk_reasons[1:] or decision.risk_reasons)
            + "). Send the request again for a new approval."
        )
    return approval


def execute_tool(
    db: Session,
    principal: AuthenticatedPrincipal,
    tool_name: str,
    arguments: Any,
    *,
    idempotency_key: str | None = None,
    conversation_id: str | None = None,
    approval_id: str | None = None,
) -> ToolExecutionOutcome:
    """Validate, guard and run one tool call as ``principal``.

    Reads run after the scope and schema checks. Writes also pass the write
    guard: a denied or conflicting call raises (and is audited); a medium- or
    high-risk call returns ``status="approval_required"`` with the pending
    approval and runs nothing, unless ``approval_id`` names a decided approval
    for exactly this call. There is no flag that skips these checks.

    A write repeated with the same idempotency key and arguments returns the
    first result (``replayed=True``) without running again.
    """
    try:
        spec = validate_tool_call(
            principal, tool_name, arguments, idempotency_key=idempotency_key
        )
    except ToolPermissionDenied as exc:
        record_audit(db, actor=principal, tool_name=tool_name, event="permission_denied",
                     conversation_id=conversation_id, detail=exc.message,
                     data={"reasons": exc.reasons})
        raise
    except UnknownToolError:
        record_audit(
            db,
            actor=principal,
            tool_name="unknown",
            event="permission_denied",
            conversation_id=conversation_id,
            detail="Unknown AI tool denied.",
        )
        raise
    except ToolArgumentError as exc:
        spec_or_none = get_registry()._tools.get(tool_name)
        if spec_or_none is not None and spec_or_none.is_write:
            record_audit(db, actor=principal, tool_name=tool_name, event="invalid_arguments",
                         conversation_id=conversation_id, detail=exc.message,
                         data={"errors": exc.errors})
        raise
    affected_people = None
    if not spec.is_write and spec.authorize is not None:
        denials = spec.authorize(db, principal, arguments)
        if denials:
            message = "Permission denied: " + " ".join(denials)
            record_audit(
                db,
                actor=principal,
                tool_name=spec.name,
                event="permission_denied",
                conversation_id=conversation_id,
                detail=message,
                data={"reasons": denials},
            )
            raise ToolPermissionDenied(message, denials)
    if spec.name == "email.send":
        if spec.authorize is not None:
            denials = spec.authorize(db, principal, arguments)
            if denials:
                message = "Permission denied: " + " ".join(denials)
                record_audit(
                    db,
                    actor=principal,
                    tool_name=spec.name,
                    event="permission_denied",
                    conversation_id=conversation_id,
                    detail=message,
                    data={"reasons": denials},
                )
                raise ToolPermissionDenied(message, denials)
        from app.email_draft_service import resolve_email_send_recipients

        try:
            resolved_arguments, affected_people = resolve_email_send_recipients(
                db,
                recipient_emails=arguments.get("recipient_emails", []),
                groups=arguments.get("groups", []),
                exclude_email=principal.email,
            )
        except ValueError as exc:
            raise ToolArgumentError(str(exc)) from exc
        arguments = {
            **arguments,
            "recipient_emails": resolved_arguments["recipient_emails"],
        }
        if "groups" in arguments:
            arguments["groups"] = []
        spec = validate_tool_call(
            principal, tool_name, arguments, idempotency_key=idempotency_key
        )
    if not spec.is_write:
        # Reads are never replayed; the key is only meaningful for writes.
        return _run_with_ledger(db, spec, principal, arguments, None, conversation_id)

    digest = arguments_hash(arguments)
    previous = (
        db.query(AssistantToolExecution)
        .filter(
            AssistantToolExecution.actor_id == principal.user_id,
            AssistantToolExecution.idempotency_key == idempotency_key,
        )
        .one_or_none()
    )
    if previous is not None and previous.status == TOOL_EXECUTION_STATUS_SUCCEEDED:
        if previous.tool_name != spec.name or previous.arguments_hash != digest:
            raise IdempotencyConflict(
                "This idempotency key was already used for a different operation. "
                "Use a new key for a new operation."
            )
        if spec.name in {"task.create", "meeting.schedule", "email.send"}:
            if not (previous.result or {}).get("verification"):
                from app.assistant_verification import verify_write_result

                previous.result = verify_write_result(
                    db,
                    principal,
                    spec.name,
                    arguments,
                    dict(previous.result or {}),
                    execution_id=previous.id,
                    conversation_id=conversation_id,
                )
        record_audit(db, actor=principal, tool_name=spec.name, event="replayed",
                     execution_id=previous.id, conversation_id=conversation_id,
                     detail="Repeated request; returned the earlier result without acting again.")
        return _outcome(previous, replayed=True)

    decision = evaluate_write(db, spec, principal, arguments, ignore_approval_id=approval_id)
    if decision.denials:
        message = "Permission denied: " + " ".join(decision.denials)
        record_audit(db, actor=principal, tool_name=spec.name, event="permission_denied",
                     approval_id=approval_id, conversation_id=conversation_id,
                     detail=message, data={"reasons": decision.denials})
        raise ToolPermissionDenied(message, decision.denials)
    if decision.conflicts:
        message = "Blocked by a conflict: " + " ".join(c["message"] for c in decision.conflicts)
        record_audit(db, actor=principal, tool_name=spec.name, event="conflict_blocked",
                     risk_level=decision.risk_level, approval_id=approval_id,
                     conversation_id=conversation_id, detail=message,
                     data={"conflicts": decision.conflicts})
        raise ToolConflictBlocked(message, decision.conflicts)

    approval: AssistantActionApproval | None = None
    if decision.risk_level != "low":
        if approval_id is None:
            approval = _request_approval(
                db, spec, principal, arguments, digest, idempotency_key,
                conversation_id, decision, affected_people=affected_people,
            )
            return ToolExecutionOutcome(
                execution_id=None,
                tool_name=spec.name,
                status=OUTCOME_APPROVAL_REQUIRED,
                result=None,
                replayed=False,
                attempts=0,
                duration_ms=None,
                idempotency_key=idempotency_key,
                risk_level=decision.risk_level,
                risk_reasons=decision.risk_reasons,
                approval=approval_as_dict(approval),
            )
        approval = _check_approval(db, spec, principal, digest, approval_id, decision)
    else:
        record_audit(db, actor=principal, tool_name=spec.name, event="auto_approved",
                     risk_level="low", conversation_id=conversation_id,
                     detail=f"{spec.summarize_call(arguments)} — low risk within your "
                            "permissions; running without approval.",
                     data={"risk_reasons": decision.risk_reasons})

    try:
        approved_by = None
        if approval is not None and approval.approver_kind == "approver":
            approved_by = {
                "id": approval.decided_by,
                "role": approval.decided_by_role,
                "team": None,
                "approval_id": approval.id,
            }
        outcome = _run_with_ledger(
            db, spec, principal, arguments, idempotency_key, conversation_id,
            approved_by=approved_by,
        )
    except ToolError as exc:
        if approval is not None:
            approval.status = ACTION_APPROVAL_STATUS_FAILED
            approval.error = exc.message[:2000]
            approval.updated_at = datetime.now(timezone.utc)
            db.commit()
        record_audit(db, actor=principal, tool_name=spec.name, event="execution_failed",
                     risk_level=decision.risk_level,
                     approval_id=approval.id if approval else None,
                     conversation_id=conversation_id, detail=exc.message)
        raise

    outcome.risk_level = decision.risk_level
    outcome.risk_reasons = decision.risk_reasons
    if outcome.result is not None and spec.name in {"task.create", "meeting.schedule", "email.send"}:
        from app.assistant_verification import verify_write_result

        outcome.result = verify_write_result(
            db,
            principal,
            spec.name,
            arguments,
            outcome.result,
            execution_id=outcome.execution_id,
            conversation_id=conversation_id,
        )
    if approval is not None:
        approval.status = ACTION_APPROVAL_STATUS_EXECUTED
        approval.execution_id = outcome.execution_id
        approval.result = outcome.result
        approval.error = None
        approval.updated_at = datetime.now(timezone.utc)
        db.commit()
        outcome.approval = approval_as_dict(approval)
    record_audit(db, actor=principal, tool_name=spec.name, event="executed",
                 risk_level=decision.risk_level,
                 approval_id=approval.id if approval else None,
                 execution_id=outcome.execution_id, conversation_id=conversation_id,
                 detail=f"{spec.summarize_call(arguments)} — done with the permissions of "
                        f"{principal.name} ({principal.role}).")
    return outcome


def _run_with_ledger(
    db: Session,
    spec: ToolSpec,
    principal: AuthenticatedPrincipal,
    arguments: dict,
    idempotency_key: str | None,
    conversation_id: str | None,
    approved_by: dict | None = None,
) -> ToolExecutionOutcome:
    """Claim the ledger row and run the handler with the timeout and retry policy."""
    tool_name = spec.name
    digest = arguments_hash(arguments)
    row, replay = _claim_ledger_row(
        db, spec, principal, arguments, digest, idempotency_key, conversation_id
    )
    if replay:
        logger.info("Replayed tool %s execution_id=%s", spec.name, row.id)
        return _outcome(row, replayed=True)

    engine = db.get_bind()
    execution_id = row.id
    progress = dict(row.progress or {})
    started = time.monotonic()
    policy = spec.retry_policy
    attempt = 0
    error: BaseException | None = None
    result: dict | None = None

    while True:
        attempt += 1
        row.attempts = (row.attempts or 0) + 1
        db.commit()
        future = _EXECUTOR.submit(
            _run_attempt, spec, engine, principal, arguments, execution_id,
            idempotency_key, conversation_id, attempt, progress, approved_by,
        )
        try:
            result = future.result(timeout=spec.timeout_seconds)
            error = None
            break
        except FutureTimeoutError:
            future.add_done_callback(lambda done: _finish_late(engine, execution_id, done))
            error = ToolTimeoutError(
                f"{tool_name!r} did not finish within {spec.timeout_seconds:g} seconds."
            )
            # A timed-out write may still complete; never start a second copy.
            break
        except BaseException as exc:  # noqa: BLE001 - classified below
            error = exc
            db.expire(row)
            progress = dict(row.progress or {})
            retryable = isinstance(exc, policy.retry_on) if policy.retry_on else False
            if retryable and attempt < policy.max_attempts:
                logger.warning(
                    "Tool %s attempt %d failed (%s); retrying.",
                    spec.name, attempt, type(exc).__name__,
                )
                _sleep(policy.delay_before(attempt + 1))
                continue
            break

    db.expire(row)
    row = db.get(AssistantToolExecution, execution_id)
    row.duration_ms = int((time.monotonic() - started) * 1000)
    row.completed_at = datetime.now(timezone.utc)
    if error is None:
        row.status = TOOL_EXECUTION_STATUS_SUCCEEDED
        row.result = result
        row.error = None
        db.commit()
        logger.info(
            "Tool %s succeeded execution_id=%s attempts=%d", spec.name, execution_id, row.attempts
        )
        return _outcome(row, replayed=False)

    if isinstance(error, ToolTimeoutError):
        if row.status == TOOL_EXECUTION_STATUS_RUNNING:
            row.status = TOOL_EXECUTION_STATUS_TIMED_OUT
    else:
        row.status = TOOL_EXECUTION_STATUS_FAILED
    row.error = str(error)[:2000]
    db.commit()
    logger.warning(
        "Tool %s %s execution_id=%s: %s", spec.name, row.status, execution_id, type(error).__name__
    )
    if isinstance(error, ToolError):
        error.execution_id = execution_id
        raise error
    if isinstance(error, OperationalError):
        transient_error = ToolTransientError("The database is busy; try again shortly.")
        transient_error.execution_id = execution_id
        raise transient_error from error
    execution_error = ToolExecutionError(
        f"{tool_name!r} failed unexpectedly ({type(error).__name__})."
    )
    execution_error.execution_id = execution_id
    raise execution_error from error
