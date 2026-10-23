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
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    "workflow.run",
})
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "employee": EMPLOYEE_SCOPES,
    "manager": MANAGER_SCOPES,
    "hr_operations": MANAGER_SCOPES,
    "admin": MANAGER_SCOPES,
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


class UnknownToolError(ToolError):
    status_code = 404


class ToolPermissionDenied(ToolError):
    status_code = 403


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


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    description: str
    input_schema: dict
    required_scopes: frozenset[str]
    risk_tier: RiskTier
    timeout_seconds: float
    retry_policy: RetryPolicy
    handler: ToolHandler
    # Cross-field rules a JSON schema cannot express (e.g. end after start).
    validator: ToolValidator | None = None

    @property
    def is_write(self) -> bool:
        return self.risk_tier != "read"

    @property
    def requires_idempotency_key(self) -> bool:
        return self.is_write

    @property
    def requires_confirmation(self) -> bool:
        return self.risk_tier in CONFIRMATION_RISK_TIERS

    def describe(self, scopes: frozenset[str] | None = None) -> dict:
        data = {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "input_schema": self.input_schema,
            "required_scopes": sorted(self.required_scopes),
            "risk_tier": self.risk_tier,
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
    execution_id: str
    tool_name: str
    status: str
    result: dict | None
    replayed: bool
    attempts: int
    duration_ms: int | None
    idempotency_key: str | None

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


def execute_tool(
    db: Session,
    principal: AuthenticatedPrincipal,
    tool_name: str,
    arguments: Any,
    *,
    idempotency_key: str | None = None,
    conversation_id: str | None = None,
    confirmed: bool = False,
) -> ToolExecutionOutcome:
    """Validate and run one tool call, honouring scopes, idempotency, timeout and retries.

    Raises a ``ToolError`` subclass when the call is rejected or fails. A write
    repeated with the same idempotency key and arguments returns the first
    result (``replayed=True``) without running again.
    """
    spec = validate_tool_call(
        principal, tool_name, arguments, idempotency_key=idempotency_key
    )
    if spec.requires_confirmation and not confirmed:
        raise ToolConfirmationRequired(
            f"The {spec.risk_tier}-risk tool {tool_name!r} needs explicit confirmation."
        )
    if not spec.is_write:
        # Reads are never replayed; the key is only meaningful for writes.
        idempotency_key = None

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
            idempotency_key, conversation_id, attempt, progress,
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
        raise error
    if isinstance(error, OperationalError):
        raise ToolTransientError("The database is busy; try again shortly.") from error
    raise ToolExecutionError(
        f"{tool_name!r} failed unexpectedly ({type(error).__name__})."
    ) from error
