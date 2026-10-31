"""Intent classification and entity extraction for assistant chat commands."""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime, time as Time, timezone
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.email_draft_service import (
    detect_ambiguous_employee_names,
    detect_groups,
    detect_named_employees,
)
from app.meeting.scheduling_service import (
    extract_meeting_title,
    get_organization_timezone,
    parse_duration_minutes,
    parse_meeting_date,
    parse_time_of_day,
)


Intent = Literal["task_assignment", "meeting_scheduling", "send_email", "unsupported"]
UnderstandingStatus = Literal["understood", "needs_clarification", "unsupported"]


class AssistantEntities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    people: list[str] = Field(default_factory=list)
    ambiguous_people: dict[str, list[str]] = Field(default_factory=dict)
    groups: list[str] = Field(default_factory=list)
    task: str | None = None
    meeting: str | None = None
    topic: str | None = None
    date: Date | None = None
    time: Time | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)


class AssistantUnderstanding(BaseModel):
    """Validated, normalized interpretation of an assistant command."""

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    status: UnderstandingStatus
    entities: AssistantEntities = Field(default_factory=AssistantEntities)
    missing_entities: list[str] = Field(default_factory=list)
    explanation: str | None = None

    @model_validator(mode="after")
    def validate_status_and_required_entities(self) -> AssistantUnderstanding:
        if self.intent == "unsupported":
            if self.status != "unsupported" or not self.explanation:
                raise ValueError("Unsupported requests require an explanation.")
            return self

        if self.status == "unsupported":
            raise ValueError("Supported intents cannot have unsupported status.")

        if self.status == "understood":
            if self.missing_entities or self.entities.ambiguous_people:
                raise ValueError("Understood requests cannot have missing entities.")
            if self.intent == "task_assignment" and not self.entities.task:
                raise ValueError("Task assignments require a task description.")
            if self.intent == "meeting_scheduling" and (
                not (self.entities.people or self.entities.groups)
                or self.entities.date is None
                or self.entities.time is None
            ):
                raise ValueError(
                    "Meeting requests require an attendee, date, and time."
                )
            if self.intent == "send_email" and (
                not (self.entities.people or self.entities.groups)
                or not self.entities.topic
            ):
                raise ValueError(
                    "E-mail requests require a recipient and a topic."
                )
        elif not self.missing_entities:
            raise ValueError(
                "Requests needing clarification must identify missing entities."
            )
        return self


_TASK_COMMAND = re.compile(
    r"\b(?:assign|reassign|allocate)\s+(?:the\s+)?(?P<task>.+?)\s+to\s+"
    r"(?P<person>.+?)(?=\s+(?:by|before|due|on)\b|[.!?,]|$)",
    re.IGNORECASE,
)
_TASK_NAMED_COMMAND = re.compile(
    r"\bassign\s+(?P<person>.+?)\s+(?:a|the)\s+task\s+(?:called|named)\s+"
    r'(?:"(?P<double_task>[^"]+)"|\'(?P<single_task>[^\']+)\'|'
    r"(?P<plain_task>.+?))(?=\s+(?:due|by|before|on)\b|[.!?,]|$)",
    re.IGNORECASE,
)
# Matches "create a task", "add a task", "make a task", "create task", "add task"
_TASK_CREATE_COMMAND = re.compile(
    r"\b(?:create|add|make|log|submit)\s+(?:a\s+|the\s+|an\s+)?task[:\s]+(?P<task>.+?)(?=\s+(?:due|by|before|on)\b|[.!?,]|$)",
    re.IGNORECASE,
)
# Also match: "create a task called X", "add task named X"
_TASK_CREATE_NAMED = re.compile(
    r"\b(?:create|add|make|log)\s+(?:a\s+|the\s+|an\s+)?task\s+(?:called|named|titled)[:\s]+(?P<task>.+?)(?=\s+(?:due|by|before|on)\b|[.!?,]|$)",
    re.IGNORECASE,
)
_TASK_INTENT = re.compile(
    r"\b(?:assign|reassign|allocate|create\s+(?:a\s+)?task|add\s+(?:a\s+)?task|make\s+(?:a\s+)?task|log\s+(?:a\s+)?task)\b",
    re.IGNORECASE,
)
_MEETING_INTENT = re.compile(
    r"\b(?:schedule|book|set\s+up)\b.*\bmeeting\b|\bmeeting\b.*"
    r"\b(?:schedule|book|set\s+up)\b",
    re.IGNORECASE,
)
_EMAIL_INTENT = re.compile(
    r"\b(?:send|e-?mail|mail)\b.*\b(?:e-?mail|mail|to|about)\b",
    re.IGNORECASE,
)
_ACTION_REQUEST = re.compile(
    r"\b(?:delete|cancel|create|update|move|reschedule|book|schedule|send|"
    r"assign|reassign|allocate|notify|email|e-mail)\b",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"\b(?:for\s+)?(\d+)\s*(?:hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)
_DATE_MODIFIER_PATTERN = (
    r"\b(?:today|tomorrow|the next day|in\s+\d+\s+days?|"
    r"(?:(?:next|this|on)\s+)?(?:monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|\d{4}-\d{2}-\d{2}|"
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+)?"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)(?:\.?\s+\d{1,2}(?:st|nd|rd|th)?)?)\b"
)
_TOPIC = re.compile(
    rf"\b(?:about|regarding|explaining|describing)\s+(.+?)"
    rf"(?=\s+(?:to|cc)\b|\s+by\s+(?={_DATE_MODIFIER_PATTERN})"
    # Story 7.3: "… according to the HR handbook" says where to look, not the topic.
    r"|\s+(?:according\s+to|based\s+on|using)\b|[.!?]|$)",
    re.IGNORECASE,
)
_DATE_MODIFIER = re.compile(
    _DATE_MODIFIER_PATTERN,
    re.IGNORECASE,
)


def _clean_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip(" \t\r\n.,;:!?")).strip()


def _recognized_employee_name(db: Session, phrase: str) -> str:
    candidates = detect_named_employees(db, phrase)
    normalized = _clean_phrase(phrase)
    for employee in candidates:
        if employee.name.casefold() == normalized.casefold():
            return employee.name
        if employee.name.split()[0].casefold() == normalized.casefold():
            return employee.name
    return normalized


def _reference_local(reference_time: datetime | None) -> datetime:
    organization_timezone = get_organization_timezone()
    reference = reference_time or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(organization_timezone)


def _intent_for(text: str) -> Intent | None:
    if _TASK_INTENT.search(text):
        return "task_assignment"
    if _MEETING_INTENT.search(text):
        return "meeting_scheduling"
    if _EMAIL_INTENT.search(text):
        return "send_email"
    if _ACTION_REQUEST.search(text):
        return "unsupported"
    return None


def _extract_people(db: Session, text: str) -> list[str]:
    return list(dict.fromkeys(employee.name for employee in detect_named_employees(db, text)))


def _extract_date(text: str, reference_local: datetime) -> Date | None:
    if not _DATE_MODIFIER.search(text):
        return None
    return parse_meeting_date(text, reference_local)


def _extract_duration(text: str) -> int | None:
    match = _DURATION.search(text)
    if not match:
        return None
    duration = parse_duration_minutes(match.group(0))
    return duration if 1 <= duration <= 1440 else None


def _missing_entities(intent: Intent, entities: AssistantEntities) -> list[str]:
    missing: list[str] = []
    if entities.ambiguous_people:
        missing.append("ambiguous employee")
    if intent == "task_assignment":
        if not entities.task:
            missing.append("task")
        # assignee is optional for create-task intent (user can self-assign)
    elif intent == "meeting_scheduling":
        if not (entities.people or entities.groups):
            missing.append("attendee or group")
        if entities.date is None:
            missing.append("date")
        if entities.time is None:
            missing.append("time")
    elif intent == "send_email":
        if not (entities.people or entities.groups):
            missing.append("recipient or group")
        if not entities.topic:
            missing.append("email topic")
    return missing


def _unsupported_result() -> AssistantUnderstanding:
    return AssistantUnderstanding(
        intent="unsupported",
        status="unsupported",
        explanation=(
            "I can understand task assignments, meeting requests, and e-mail "
            "requests here, but I can't handle that operation yet. "
            "I'm only able to help with office operations: scheduling meetings, "
            "managing tasks, drafting emails, or checking your calendar."
        ),
    )


def understand_assistant_command(
    db: Session,
    text: str,
    reference_time: datetime | None = None,
) -> AssistantUnderstanding | None:
    """Classify supported commands and return their schema-validated entities.

    Returns ``None`` for general conversation so the existing chat completion
    flow can handle it.
    """
    intent = _intent_for(text)
    if intent is None:
        return None
    if intent == "unsupported":
        return _unsupported_result()

    reference_local = _reference_local(reference_time)
    groups = detect_groups(db, text)
    people = _extract_people(db, text)
    ambiguous_people = detect_ambiguous_employee_names(db, text)
    task = None
    meeting = None
    topic = None

    if intent == "task_assignment":
        requested_assignee = ""
        # Check explicit "create/add task: <description>" style first
        create_match = _TASK_CREATE_NAMED.search(text) or _TASK_CREATE_COMMAND.search(text)
        if create_match:
            task = _clean_phrase(create_match.group("task"))
            # For create-task, the logged-in user is the default assignee; no explicit person needed
        else:
            # Fall back to classic assign patterns
            match = _TASK_NAMED_COMMAND.search(text)
            if match:
                task = _clean_phrase(
                    match.group("double_task")
                    or match.group("single_task")
                    or match.group("plain_task")
                )
                requested_assignee = _clean_phrase(match.group("person"))
            else:
                match = _TASK_COMMAND.search(text)
                if match:
                    task = _clean_phrase(match.group("task"))
                    requested_assignee = _clean_phrase(match.group("person"))
            if requested_assignee:
                if requested_assignee.casefold() not in {
                    name.casefold() for name in ambiguous_people
                }:
                    people = [_recognized_employee_name(db, requested_assignee)]
    elif intent == "meeting_scheduling":
        meeting = extract_meeting_title(text)
        if meeting.casefold() == "project meeting":
            meeting = "Meeting"
    elif intent == "send_email":
        topic_match = _TOPIC.search(text)
        if topic_match:
            topic = _clean_phrase(topic_match.group(1))

    time_of_day = parse_time_of_day(text)
    twelve_hour_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        text,
        re.IGNORECASE,
    )
    valid_twelve_hour_time = (
        twelve_hour_match is None
        or (
            1 <= int(twelve_hour_match.group(1)) <= 12
            and int(twelve_hour_match.group(2) or 0) <= 59
        )
    )
    parsed_time = (
        Time(hour=time_of_day[0], minute=time_of_day[1])
        if time_of_day is not None
        and valid_twelve_hour_time
        and 0 <= time_of_day[0] <= 23
        and 0 <= time_of_day[1] <= 59
        else None
    )
    duration = _extract_duration(text)
    has_invalid_duration = _DURATION.search(text) is not None and duration is None

    entities = AssistantEntities(
        people=people,
        ambiguous_people=ambiguous_people,
        groups=groups,
        task=task,
        meeting=meeting,
        topic=topic,
        date=_extract_date(text, reference_local),
        time=parsed_time,
        duration_minutes=duration,
    )
    missing = _missing_entities(intent, entities)
    if has_invalid_duration:
        missing.append("a valid duration (1 to 1440 minutes)")
    status: UnderstandingStatus = (
        "needs_clarification" if missing else "understood"
    )
    return AssistantUnderstanding(
        intent=intent,
        status=status,
        entities=entities,
        missing_entities=missing,
    )


def explain_understanding(result: AssistantUnderstanding) -> str:
    """Format a validated understanding as a plain-language assistant reply."""
    if result.status == "unsupported":
        return result.explanation or "I can't handle that request yet."

    if result.status == "needs_clarification":
        needed = ", ".join(result.missing_entities)
        return (
            f"I understood this as a {result.intent.replace('_', ' ')} request, "
            f"but I still need {needed}. I haven't made any changes."
        )

    entities = result.entities
    if result.intent == "task_assignment":
        assignee = entities.people[0] if entities.people else "yourself"
        details = (
            f'the task "{entities.task}" to {assignee}'
            + (f" by {_format_date(entities.date)}" if entities.date else "")
        )
    elif result.intent == "meeting_scheduling":
        recipient = (entities.groups or entities.people)[0]
        details = (
            f"a meeting with {recipient} on {_format_date(entities.date)} "
            f"at {_format_time(entities.time)}"
        )
        if entities.duration_minutes is not None:
            details += f" for {entities.duration_minutes} minutes"
    else:
        recipient = (entities.groups or entities.people)[0]
        details = f'an e-mail to {recipient} about "{entities.topic}"'
    return f"I understood this as a request for {details}. I haven't made any changes."


def clarification_target(result: AssistantUnderstanding) -> str:
    """Return the one unresolved field to ask about next."""
    if result.entities.ambiguous_people:
        name = next(iter(result.entities.ambiguous_people))
        return f"ambiguous:{name}"
    return result.missing_entities[0]


def clarification_question(result: AssistantUnderstanding) -> str:
    """Ask a focused question about the next unresolved field."""
    if result.entities.ambiguous_people:
        name = next(iter(result.entities.ambiguous_people))
        candidates = result.entities.ambiguous_people[name]
        choices = ", ".join(candidates)
        return f"I found multiple employees named {name}: {choices}. Which one did you mean?"

    target = clarification_target(result)
    if result.intent == "meeting_scheduling":
        if "date" in result.missing_entities and "time" in result.missing_entities:
            if "attendee or group" in result.missing_entities:
                return "What date and time do you need to schedule the meeting for, and who should attend?"
            return "What date and time do you need to schedule the meeting for?"
    if target == "task":
        return "What task would you like to create? Please say the task to create (e.g., 'Review sprint demo deliverables by Friday')."
    if target == "assignee":
        return f"Who should I assign {result.entities.task or 'the task'} to?"
    if target == "attendee or group":
        return "Who should attend the meeting?"
    if target == "date":
        return "What date should I schedule the meeting?"
    if target == "time":
        return "What time should I schedule the meeting?"
    if target == "recipient or group":
        return "Who should receive the e-mail?"
    if target == "email topic":
        return "What should the e-mail be about?"
    if target.startswith("a valid duration"):
        return "What duration should I use, between 1 and 1440 minutes?"
    return f"What value should I use for {target}?"


def merge_clarification_answer(
    db: Session,
    result: AssistantUnderstanding,
    target: str,
    answer: str,
    original_prompt: str,
    reference_time: datetime | None = None,
) -> AssistantUnderstanding:
    """Apply an answer to the pending field and revalidate with the shared parser."""
    if target.startswith("ambiguous:"):
        ambiguous_name = target.removeprefix("ambiguous:")
        candidates = result.entities.ambiguous_people.get(ambiguous_name, [])
        selected = next(
            (candidate for candidate in candidates if candidate.casefold() == answer.strip().casefold()),
            None,
        )
        if selected is None and answer.strip().isdigit():
            selection = int(answer.strip()) - 1
            if 0 <= selection < len(candidates):
                selected = candidates[selection]
        if selected is None:
            return result
        resolved_prompt = re.sub(
            rf"\b{re.escape(ambiguous_name)}\b",
            selected,
            original_prompt,
            count=1,
            flags=re.IGNORECASE,
        )
        return understand_assistant_command(db, resolved_prompt, reference_time) or result

    entities = result.entities
    task = entities.task or ""
    assignee = entities.people[0] if entities.people else ""
    recipient = (entities.groups or entities.people)
    recipient_name = recipient[0] if recipient else ""
    date_text = entities.date.isoformat() if entities.date else ""
    time_text = entities.time.strftime("%I:%M %p").lstrip("0") if entities.time else ""
    duration_text = (
        f" for {entities.duration_minutes} minutes"
        if entities.duration_minutes is not None
        else ""
    )

    if result.intent == "task_assignment":
        if target == "task":
            candidate = understand_assistant_command(db, f"Create a task: {answer}", reference_time)
            if candidate is not None and candidate.intent == "task_assignment" and candidate.entities.task:
                task = candidate.entities.task
                if candidate.entities.date:
                    entities.date = candidate.entities.date
                    date_text = candidate.entities.date.isoformat()
            else:
                task = answer
        elif target == "assignee":
            assignee = answer
        if assignee:
            probe = f"Assign {task} to {assignee}"
        else:
            probe = f"Create task: {task}"
        if entities.date:
            probe += f" by {date_text}"
    elif result.intent == "meeting_scheduling":
        # Check if answer provides meeting details (attendee, date, time) directly
        candidate = understand_assistant_command(db, f"Schedule a meeting {answer}", reference_time)
        if candidate is not None and candidate.intent == "meeting_scheduling":
            if candidate.entities.people or candidate.entities.groups or candidate.entities.date or candidate.entities.time:
                merged_people = candidate.entities.people or entities.people
                merged_groups = candidate.entities.groups or entities.groups
                merged_date = candidate.entities.date or entities.date
                merged_time = candidate.entities.time or entities.time
                merged_duration = candidate.entities.duration_minutes or entities.duration_minutes

                recip = ""
                if merged_groups:
                    g = merged_groups[0]
                    recip = "everyone" if g.casefold() == "everyone" else f"the {g} team"
                elif merged_people:
                    recip = merged_people[0]

                title = entities.meeting or "Meeting"
                p = f"Schedule a {title} meeting"
                if recip:
                    p += f" with {recip}"
                if merged_date:
                    p += f" on {merged_date.isoformat()}"
                if merged_time:
                    p += f" at {merged_time.strftime('%I:%M %p').lstrip('0')}"
                if merged_duration:
                    p += f" for {merged_duration} minutes"

                resolved = understand_assistant_command(db, p, reference_time)
                if resolved is not None:
                    return resolved

        if target == "attendee or group":
            recipient_name = answer
        elif target == "date":
            date_text = answer
        elif target == "time":
            time_text = answer
        elif target.startswith("a valid duration"):
            duration_text = f" for {answer}"
        if target != "attendee or group" and entities.groups:
            group = entities.groups[0]
            recipient_name = (
                "everyone" if group.casefold() == "everyone" else f"the {group} team"
            )
        title = entities.meeting or "Meeting"
        probe = f"Schedule a {title} meeting"
        if recipient_name:
            probe += f" with {recipient_name}"
        if date_text:
            probe += f" on {date_text}"
        if time_text:
            probe += f" at {time_text}"
        probe += duration_text
    else:
        if target == "recipient or group":
            recipient_name = answer
        elif target == "email topic":
            entities.topic = answer
        topic = entities.topic or ""
        probe = f"Send an e-mail to {recipient_name} about {topic}"

    return understand_assistant_command(db, probe, reference_time) or result


def _format_date(value: Date) -> str:
    return value.strftime("%B %d").replace(" 0", " ")


def _format_time(value: Time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")
