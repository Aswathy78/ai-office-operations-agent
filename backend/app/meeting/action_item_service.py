"""Generation and persistence of timestamped meeting action items."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Literal
import uuid

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.employee_matching import EmployeeMatchResult, match_employee_name
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_EXTRACTION_STATUS_FAILED,
    ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    ACTION_ITEM_EXTRACTION_STATUS_PROCESSING,
    ACTION_ITEM_PRIORITIES,
    ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    ACTION_ITEM_REVIEW_STATUS_REJECTED,
    EMPLOYEE_MATCH_STATUS_AMBIGUOUS,
    EMPLOYEE_MATCH_STATUS_MISSING_OWNER,
    EMPLOYEE_MATCH_STATUS_NO_MATCH,
    EMPLOYEE_MATCH_STATUS_UNIQUE,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_APPROVAL_REQUIRED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_PROPOSED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    TRANSCRIPT_STATUS_COMPLETED,
)
from app.meeting.schemas import TranscriptSegment
from app.task_service import (
    attempt_assignment_email,
    prepare_task_from_confirmed_action_item,
)


logger = logging.getLogger(__name__)

DEFAULT_ACTION_ITEM_CONFIDENCE_THRESHOLD = 0.85


def get_action_item_confidence_threshold() -> float:
    """Retrieve the configurable action-item confidence threshold (defaults to 0.85)."""
    raw = os.getenv("ACTION_ITEM_CONFIDENCE_THRESHOLD")
    if raw is None or not raw.strip():
        return DEFAULT_ACTION_ITEM_CONFIDENCE_THRESHOLD
    try:
        val = float(raw.strip())
        if 0.0 <= val <= 1.0:
            return val
        logger.warning(
            "ACTION_ITEM_CONFIDENCE_THRESHOLD %r is outside [0.0, 1.0], using default %f",
            raw,
            DEFAULT_ACTION_ITEM_CONFIDENCE_THRESHOLD,
        )
        return DEFAULT_ACTION_ITEM_CONFIDENCE_THRESHOLD
    except (ValueError, TypeError):
        logger.warning(
            "Invalid ACTION_ITEM_CONFIDENCE_THRESHOLD %r, using default %f",
            raw,
            DEFAULT_ACTION_ITEM_CONFIDENCE_THRESHOLD,
        )
        return DEFAULT_ACTION_ITEM_CONFIDENCE_THRESHOLD

def _resolve_deadline_text(
    deadline_text: str | None,
    meeting_start_at: datetime,
    organization_timezone: ZoneInfo,
) -> datetime | None:
    """Resolve supported explicit and relative deadlines in the organization zone.

    Unsupported or ambiguous phrases return None so they can be reviewed.
    """
    if not deadline_text or not deadline_text.strip():
        return None

    if meeting_start_at.tzinfo is None:
        # SQLite may return UTC datetimes without timezone information.
        meeting_start_at = meeting_start_at.replace(tzinfo=timezone.utc)

    local_meeting_start = meeting_start_at.astimezone(
        organization_timezone
    )

    meeting_date = local_meeting_start.date()
    phrase = deadline_text.strip().lower()
    phrase = re.sub(r"[.,!?]+$", "", phrase).strip()

    # "Next week" alone is a period, not an exact deadline.
    if phrase in {"next week", "sometime next week", "during next week"}:
        return None

    # "By next Friday" means Friday of the following calendar week.
    if phrase in {"by next friday", "next friday"}:
        days_until_next_monday = 7 - meeting_date.weekday()
        deadline_date = meeting_date + timedelta(
            days=days_until_next_monday + 4
        )
        return datetime.combine(
            deadline_date,
            datetime.min.time(),
            tzinfo=organization_timezone,
        )

    # Remove common deadline prefixes.
    phrase = re.sub(
        r"^(?:by|on|due|due on|due by)\s+",
        "",
        phrase,
    ).strip()

    absolute_match = re.fullmatch(
        r"(?:(?P<day_first>\d{1,2})\s+(?P<day_month>[a-z]+),?\s+"
        r"(?P<day_year>\d{4})|(?P<month_first>[a-z]+)\s+"
        r"(?P<month_day>\d{1,2}),?\s+(?P<month_year>\d{4}))"
        r"(?:\s+at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
        r"(?P<meridiem>am|pm))?",
        phrase,
    )
    if absolute_match:
        if absolute_match.group("day_first") is not None:
            date_text = (
                f"{absolute_match.group('day_first')} "
                f"{absolute_match.group('day_month')} "
                f"{absolute_match.group('day_year')}"
            )
            date_formats = ("%d %B %Y", "%d %b %Y")
        else:
            date_text = (
                f"{absolute_match.group('month_first')} "
                f"{absolute_match.group('month_day')} "
                f"{absolute_match.group('month_year')}"
            )
            date_formats = ("%B %d %Y", "%b %d %Y")

        deadline_date = None
        for date_format in date_formats:
            try:
                deadline_date = datetime.strptime(date_text, date_format).date()
                break
            except ValueError:
                continue
        if deadline_date is None:
            return None

        hour_text = absolute_match.group("hour")
        minute_text = absolute_match.group("minute") or "00"
        meridiem = absolute_match.group("meridiem")
        if hour_text is None:
            deadline_time = datetime.min.time()
        else:
            try:
                deadline_time = datetime.strptime(
                    f"{hour_text}:{minute_text} {meridiem}",
                    "%I:%M %p",
                ).time()
            except ValueError:
                return None

        return datetime.combine(
            deadline_date,
            deadline_time,
            tzinfo=organization_timezone,
        )

    # "Tomorrow" means the next calendar day.
    if phrase in {"tomorrow", "the next day"}:
        deadline_date = meeting_date + timedelta(days=1)

    # "Today" means the meeting's local calendar date.
    elif phrase in {"today", "by today"}:
        deadline_date = meeting_date

    # "In N days" / "N days from now".
    else:
        days_match = re.fullmatch(
            r"(?:in\s+)?(\d+)\s+days?(?:\s+from\s+now)?",
            phrase,
        )
        if days_match:
            number_of_days = int(days_match.group(1))

            # Avoid unreasonable or accidental dates.
            if not 0 <= number_of_days <= 365:
                return None

            deadline_date = meeting_date + timedelta(
                days=number_of_days
            )

        else:
            # Weekday names: Monday through Sunday.
            weekdays = {
                "monday": 0,
                "tuesday": 1,
                "wednesday": 2,
                "thursday": 3,
                "friday": 4,
                "saturday": 5,
                "sunday": 6,
            }

            # Support "this Friday" and "by Friday".
            weekday_phrase = re.sub(
                r"^(?:this|coming)\s+",
                "",
                phrase,
            ).strip()

            if weekday_phrase in weekdays:
                target_weekday = weekdays[weekday_phrase]
                days_ahead = (
                    target_weekday - meeting_date.weekday()
                ) % 7

                # If the meeting is on that weekday, use the same day.
                deadline_date = meeting_date + timedelta(
                    days=days_ahead
                )

            else:
                # Unknown or ambiguous phrases must be reviewed.
                return None

    return datetime.combine(
        deadline_date,
        datetime.min.time(),
        tzinfo=organization_timezone,
    )

class ActionItemCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_type: Literal["assignment", "commitment", "request"]
    owner_name: str | None = Field(default=None, max_length=255)
    task_description: str = Field(min_length=1)
    deadline_at: datetime | None = None
    deadline_text: str | None = None
    priority: Literal["low", "medium", "high", "urgent"]
    confidence: float = Field(ge=0, le=1)
    source_segment_index: int = Field(ge=0, strict=True)
    source_quote: str = Field(min_length=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence_is_numeric(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Confidence must be numeric.")
        return value


class ActionItemCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_items: list[ActionItemCandidate]


class MeetingActionItemExtractionError(RuntimeError):
    """Raised when persisted meeting action items cannot be generated."""


class _SafeExtractionFailure(ValueError):
    """An extraction failure whose message is safe to persist."""


def _annotate_employee_match(
    db: Session,
    item: MeetingActionItem,
) -> EmployeeMatchResult:
    """Match an extracted owner against the active employee directory."""
    match_result = match_employee_name(db, item.owner_name)
    item.employee_match_status = match_result.status
    item.proposed_employee_id = match_result.employee_id
    item.ownership_status = _ownership_status_for_match(match_result)
    setattr(item, "employee_match_result", match_result)
    setattr(item, "proposed_employee_name", match_result.employee_name)
    return match_result


def _ownership_status_for_match(match_result: EmployeeMatchResult) -> str:
    if match_result.status == "missing_owner":
        return MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED
    if match_result.status == "unique_match":
        return MEETING_ACTION_ITEM_OWNERSHIP_STATUS_PROPOSED
    if match_result.status == "ambiguous_match":
        return MEETING_ACTION_ITEM_OWNERSHIP_STATUS_APPROVAL_REQUIRED
    if match_result.status == "no_match":
        return MEETING_ACTION_ITEM_OWNERSHIP_STATUS_APPROVAL_REQUIRED
    return MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED


def refresh_missing_employee_matches(
    db: Session,
    extraction: MeetingActionItemExtraction,
) -> MeetingActionItemExtraction:
    """Backfill match metadata on completed extractions created before matching.

    Unconfirmed "no_match" items are re-matched too, so employees added to the
    directory after extraction become available as proposed owners.
    """
    updated = False
    for item in extraction.items:
        if item.employee_match_status is not None:
            if (
                item.employee_match_status != "no_match"
                or item.confirmed_employee_id is not None
                or item.ownership_status == MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED
            ):
                continue
            match_result = match_employee_name(db, item.owner_name)
            if match_result.status == "no_match":
                continue
        else:
            match_result = match_employee_name(db, item.owner_name)
        item.employee_match_status = match_result.status
        item.proposed_employee_id = match_result.employee_id
        if item.ownership_status != MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED:
            item.ownership_status = _ownership_status_for_match(match_result)
        setattr(item, "employee_match_result", match_result)
        setattr(item, "proposed_employee_name", match_result.employee_name)
        updated = True

    if updated:
        db.commit()
        db.refresh(extraction)
    return extraction


def _normalize_quote(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _quote_tokens(quote: str) -> list[str]:
    return re.findall(r"\w+", quote.casefold())


def _quote_crosses_boundary(
    quote: str,
    left_text: str,
    right_text: str,
) -> bool:
    quote_tokens = _quote_tokens(quote)
    left_tokens = _quote_tokens(left_text)
    right_tokens = _quote_tokens(right_text)
    combined_tokens = left_tokens + right_tokens
    boundary = len(left_tokens)
    return any(
        start < boundary < start + len(quote_tokens)
        and combined_tokens[start:start + len(quote_tokens)] == quote_tokens
        for start in range(len(combined_tokens) - len(quote_tokens) + 1)
    )


def _find_quote_timestamps(
    quote: str,
    segment: TranscriptSegment,
) -> tuple[float, float] | None:
    if not segment.words:
        return None

    quote_words = re.findall(r"\w+", quote.casefold())
    if not quote_words:
        return None

    transcript_words: list[tuple[str, float, float]] = []
    for word in segment.words:
        if (
            not math.isfinite(word.start)
            or not math.isfinite(word.end)
            or word.start < segment.start
            or word.end < word.start
            or word.end > segment.end
        ):
            return None
        tokens = re.findall(r"\w+", word.word.casefold())
        transcript_words.extend((token, word.start, word.end) for token in tokens)

    matches = [
        index
        for index in range(len(transcript_words) - len(quote_words) + 1)
        if [word[0] for word in transcript_words[index:index + len(quote_words)]]
        == quote_words
    ]
    if len(matches) != 1:
        return None

    match = matches[0]
    return (
        transcript_words[match][1],
        transcript_words[match + len(quote_words) - 1][2],
    )


def _has_actionable_intent(quote: str) -> bool:
    if re.search(
        r"^\s*(?:if|unless|assuming|provided that)\b[^.!?]*[,;]\s*"
        r"(?:(?:then)\s+)?(?:i|we|you|he|she|they|"
        r"[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+)?)\s+"
        r"(?:will|shall|should|would|might|may)\b",
        quote,
        flags=re.IGNORECASE,
    ):
        return False

    actionable_patterns = (
        r"\bplease\b",
        r"\b(?:can|could|would|will)\s+you\b",
        r"\b(?:i|we|you|he|she|they)\s+(?:will|shall|should|must|need to|have to|"
        r"am going to|are going to)\b",
        r"\b(?:i|we|you|he|she|they)['’](?:ll|d)\b",
        r"\b(?:assign(?:ed|ment)?|responsible for|owner is|owns)\b",
    )
    if any(re.search(pattern, quote, flags=re.IGNORECASE) for pattern in actionable_patterns):
        return True

    return re.search(
        r"\b[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?\s+"
        r"(?:will|shall|should|must|needs? to|has to|is going to|to)\b",
        quote,
    ) is not None


def _validated_segments(transcript: MeetingTranscript) -> list[TranscriptSegment]:
    if transcript.segments is None:
        raise _SafeExtractionFailure(
            "The completed meeting transcript has no timestamped segments."
        )

    try:
        segments = [
            TranscriptSegment.model_validate(segment)
            for segment in transcript.segments
        ]
    except ValidationError as error:
        raise _SafeExtractionFailure(
            "The completed meeting transcript has invalid segments."
        ) from error
    for segment in segments:
        if (
            not math.isfinite(segment.start)
            or not math.isfinite(segment.end)
            or segment.start < 0
            or segment.end < segment.start
        ):
            raise _SafeExtractionFailure(
                "The completed meeting transcript has invalid segment timestamps."
            )
    return segments


def _candidate_source_text(
    quote: str,
    segment_index: int,
    segments: list[TranscriptSegment],
) -> str | None:
    segment_text = segments[segment_index].text
    normalized_quote = _normalize_quote(quote)
    if (
        normalized_quote
        and f" {normalized_quote} " in f" {_normalize_quote(segment_text)} "
    ):
        return segment_text

    if segment_index > 0:
        previous_text = segments[segment_index - 1].text
        if _quote_crosses_boundary(quote, previous_text, segment_text):
            return f"{previous_text.rstrip()} {segment_text.lstrip()}"

    if segment_index + 1 < len(segments):
        next_text = segments[segment_index + 1].text
        if _quote_crosses_boundary(quote, segment_text, next_text):
            return f"{segment_text.rstrip()} {next_text.lstrip()}"
    return None


def _quote_sentence_context(quote: str, evidence_text: str) -> str:
    """Return the sentence(s) of the evidence that contain the quote.

    The model is asked for the shortest quote, which can omit the words that
    make it an assignment (e.g. "...is assigned to Rahul"), so intent is
    checked against the whole spoken sentence rather than the quote alone.
    """
    normalized_quote = _normalize_quote(quote)
    sentences = re.split(r"(?<=[.!?])\s+", evidence_text.strip())
    for start in range(len(sentences)):
        for end in range(start + 1, len(sentences) + 1):
            context = " ".join(sentences[start:end])
            if f" {normalized_quote} " in f" {_normalize_quote(context)} ":
                return context
    return quote


def _validate_candidates(
    candidates: ActionItemCandidates,
    segments: list[TranscriptSegment],
    meeting_start_at: datetime,
    organization_timezone: ZoneInfo,
) -> list[tuple[ActionItemCandidate, TranscriptSegment, tuple[float, float] | None]]:
    validated: list[
        tuple[ActionItemCandidate, TranscriptSegment, tuple[float, float] | None]
    ] = []
    seen: set[tuple[int, str]] = set()
    unsupported_quote_count = 0

    for candidate_index, candidate in enumerate(candidates.action_items):
        if candidate.source_segment_index >= len(segments):
            raise _SafeExtractionFailure(
                "An action item references a missing transcript segment."
            )

        segment = segments[candidate.source_segment_index]
        quote = candidate.source_quote.strip()
        evidence_text = _candidate_source_text(
            quote,
            candidate.source_segment_index,
            segments,
        )
        if not quote or evidence_text is None:
            unsupported_quote_count += 1
            logger.warning(
                "Discarding action-item candidate with unsupported source quote "
                "candidate_index=%d source_segment_index=%d quote_token_count=%d "
                "indexed_segment_token_count=%d",
                candidate_index,
                candidate.source_segment_index,
                len(_quote_tokens(quote)),
                len(_quote_tokens(segment.text)),
            )
            continue

        task_description = candidate.task_description.strip()
        if not task_description:
            raise _SafeExtractionFailure(
                "An action item must include a task description."
            )

        if not _has_actionable_intent(quote) and not _has_actionable_intent(
            _quote_sentence_context(quote, evidence_text)
        ):
            continue
        quote_timestamps = _find_quote_timestamps(quote, segment)

        deadline_text = candidate.deadline_text

        if deadline_text and (
            _normalize_quote(deadline_text)
            not in _normalize_quote(evidence_text)
        ):
            deadline_text = None

        # Never trust a model-generated date without validating the spoken evidence.
        deadline_at = None

        if deadline_text:
            deadline_at = _resolve_deadline_text(
                deadline_text=deadline_text,
                meeting_start_at=meeting_start_at,
                organization_timezone=organization_timezone,
            )
            if deadline_at is not None:
                deadline_at = deadline_at.astimezone(timezone.utc)


        print(
           "[DEADLINE DEBUG]",
           "text =", repr(deadline_text),
           "resolved =", repr(deadline_at),
           "meeting_start =", repr(meeting_start_at),
           "timezone =", str(organization_timezone),
        )
        candidate = candidate.model_copy(
            update={
                "deadline_text": deadline_text,
                "deadline_at": deadline_at,
            }
        )

        owner_name = candidate.owner_name.strip() if candidate.owner_name else None
        if owner_name and (
            _normalize_quote(owner_name) != _normalize_quote(segment.speaker)
            and _normalize_quote(owner_name) not in _normalize_quote(evidence_text)
        ):
            candidate = candidate.model_copy(update={"owner_name": None})
        else:
            candidate = candidate.model_copy(update={"owner_name": owner_name})
        candidate = candidate.model_copy(update={"task_description": task_description})

        duplicate_key = (
            candidate.source_segment_index,
            candidate.task_description.casefold(),
        )
        if duplicate_key not in seen:
            validated.append((candidate, segment, quote_timestamps))
            seen.add(duplicate_key)

    if unsupported_quote_count and not validated:
        raise _SafeExtractionFailure(
            "No action-item candidates had verifiable source transcript quotes."
        )

    full_segment_quote_counts: dict[tuple[int, str], int] = {}
    for candidate, segment, _quote_timestamps in validated:
        if _normalize_quote(candidate.source_quote) == _normalize_quote(segment.text):
            key = (
                candidate.source_segment_index,
                _normalize_quote(candidate.source_quote),
            )
            full_segment_quote_counts[key] = full_segment_quote_counts.get(key, 0) + 1

    return [
        (
            candidate,
            segment,
            None
            if _normalize_quote(candidate.source_quote) == _normalize_quote(segment.text)
            and full_segment_quote_counts[
                (
                    candidate.source_segment_index,
                    _normalize_quote(candidate.source_quote),
                )
            ] > 1
            else quote_timestamps,
        )
        for candidate, segment, quote_timestamps in validated
    ]


def _create_extraction(
    db: Session,
    meeting_id: str,
    transcript_id: str,
) -> MeetingActionItemExtraction:
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        status=ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    )

    db.add(extraction)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(MeetingActionItemExtraction)
            .filter(MeetingActionItemExtraction.transcript_id == transcript_id)
            .one_or_none()
        )
        if existing is None:
            raise
        return existing
    db.refresh(extraction)
    return extraction


def _refresh_existing_quote_timestamps(
    db: Session,
    extraction: MeetingActionItemExtraction,
    transcript: MeetingTranscript,
) -> MeetingActionItemExtraction:
    items_without_exact_times = [
        item
        for item in extraction.items
        if item.source_quote
        and item.source_quote_start_seconds is None
        and item.source_quote_end_seconds is None
    ]
    if not items_without_exact_times:
        return extraction

    segments = _validated_segments(transcript)
    full_segment_quote_counts: dict[tuple[int, str], int] = {}
    for item in items_without_exact_times:
        if item.source_segment_index >= len(segments) or not item.source_quote:
            continue
        segment = segments[item.source_segment_index]
        if _normalize_quote(item.source_quote) == _normalize_quote(segment.text):
            key = (
                item.source_segment_index,
                _normalize_quote(item.source_quote),
            )
            full_segment_quote_counts[key] = full_segment_quote_counts.get(key, 0) + 1

    updated = False
    for item in items_without_exact_times:
        if item.source_segment_index >= len(segments):
            continue
        quote_timestamps = _find_quote_timestamps(
            item.source_quote or "",
            segments[item.source_segment_index],
        )
        if (
            _normalize_quote(item.source_quote or "")
            == _normalize_quote(segments[item.source_segment_index].text)
            and full_segment_quote_counts.get(
                (
                    item.source_segment_index,
                    _normalize_quote(item.source_quote or ""),
                ),
                0,
            )
            > 1
        ):
            quote_timestamps = None
        if quote_timestamps is None:
            continue
        item.source_quote_start_seconds = quote_timestamps[0]
        item.source_quote_end_seconds = quote_timestamps[1]
        updated = True

    if updated:
        db.commit()
        db.refresh(extraction)
    return extraction


def generate_meeting_action_items(
    db: Session,
    transcript_id: str,
) -> MeetingActionItemExtraction:
    transcript = db.get(MeetingTranscript, transcript_id)
    if transcript is None:
        raise ValueError("Meeting transcript not found.")
    if transcript.status != TRANSCRIPT_STATUS_COMPLETED:
        raise ValueError("A completed meeting transcript is required.")

    recording = db.get(MeetingRecording, transcript.recording_id)
    if recording is None:
        raise ValueError("Meeting transcript recording not found.")
    meeting = db.get(Meeting, recording.meeting_id)
    if meeting is None:
        raise ValueError("Meeting associated with transcript was not found.")

    load_dotenv(
        dotenv_path=Path(__file__).resolve().parents[2] / ".env",
        override=False,
    )

    timezone_name = os.getenv("ORGANIZATION_TIMEZONE", "UTC")

    try:
        organization_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.exception(
            "Invalid ORGANIZATION_TIMEZONE configuration: %s",
            timezone_name,
        )
        organization_timezone = ZoneInfo("UTC")


    extraction = (
        db.query(MeetingActionItemExtraction)
        .filter(MeetingActionItemExtraction.transcript_id == transcript.id)
        .one_or_none()
    )
    if extraction is None:
        extraction = _create_extraction(db, recording.meeting_id, transcript.id)
    if extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED:
                extraction = _refresh_existing_quote_timestamps(db, extraction, transcript)
                return refresh_missing_employee_matches(db, extraction)
    if extraction.status == ACTION_ITEM_EXTRACTION_STATUS_PROCESSING:
        return extraction

    extraction.status = ACTION_ITEM_EXTRACTION_STATUS_PROCESSING
    extraction.processing_started_at = datetime.now(timezone.utc)
    extraction.completed_at = None
    extraction.error_message = None
    db.commit()

    try:
        segments = _validated_segments(transcript)
        candidates: list[
            tuple[ActionItemCandidate, TranscriptSegment, tuple[float, float] | None]
        ]
        if not segments:
            candidates = []
        else:
            load_dotenv(
                dotenv_path=Path(__file__).resolve().parents[2] / ".env",
                override=False,
            )
            api_key = os.environ.get("GROQ_API_KEY")
            if api_key:
                from groq import Groq

                client = Groq(api_key=api_key)
                model = os.environ.get(
                    "GROQ_ACTION_ITEM_MODEL",
                    os.environ.get("GROQ_SUMMARY_MODEL", "llama-3.3-70b-versatile"),
                )
            elif os.environ.get("OPENAI_API_KEY"):
                from openai import OpenAI

                client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
                model = os.environ.get("OPENAI_ACTION_ITEM_MODEL", "gpt-4o-mini")
            else:
                raise RuntimeError("GROQ_API_KEY is not set.")
            segment_payload = [
                {
                    "index": index,
                    "start": segment.start,
                    "end": segment.end,
                    "speaker": segment.speaker,
                    "text": segment.text,
                }
                for index, segment in enumerate(segments)
            ]
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract only genuine actionable assignments, explicit "
                            "commitments, or actionable requests from the supplied "
                            "timestamped transcript segments. Do not create items "
                            "from general discussion, facts, hypothetical plans, "
                            "questions without a task, unrelated conversation, or "
                            "unassigned possibilities. Never infer an owner, task, "
                            "deadline, or evidence. When one commitment is cut at "
                            "an adjacent segment boundary, source_quote may combine "
                            "verbatim text from the current segment and its adjacent "
                            "segment. Set source_segment_index to the segment where "
                            "the action task begins. For all other items, source_quote "
                            "must be verbatim from its indexed segment. Return only "
                            "source_segment_index values present in the input. Keep "
                            "source_quote to "
                            "the shortest phrase that states that individual action; "
                            "do not quote the whole segment when it contains multiple "
                            "actions. Use null for unknown "
                            "owner_name, deadline_at, or deadline_text. Set "
                            "deadline_at only to an explicit unambiguous ISO-8601 "
                            "date/time stated in the transcript; preserve the spoken "
                            "deadline in deadline_text. Priority must be one of "
                            f"{', '.join(ACTION_ITEM_PRIORITIES)}. Confidence must "
                            "be a number between 0 and 1. intent_type must be "
                            "'assignment', 'commitment', or 'request'. Return a JSON "
                            "object with exactly one field, action_items, containing "
                            "the candidate list. Each candidate must have exactly "
                            "intent_type, owner_name, task_description, deadline_at, "
                            "deadline_text, priority, confidence, "
                            "source_segment_index, and source_quote."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(segment_payload, ensure_ascii=False),
                    },
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise _SafeExtractionFailure("Groq returned no action-item data.")
            try:
                parsed_candidates = ActionItemCandidates.model_validate(
                    json.loads(content)
                )
            except (json.JSONDecodeError, ValidationError, TypeError) as error:
                raise _SafeExtractionFailure(
                    "Groq returned malformed action-item data."
                ) from error
            candidates = _validate_candidates(
                parsed_candidates,
                segments,
                meeting_start_at=meeting.start_at,
                organization_timezone=organization_timezone,
            )

        db.query(MeetingActionItem).filter(
            MeetingActionItem.extraction_id == extraction.id
        ).delete(synchronize_session=False)
        match_results_by_item_id: dict[str, EmployeeMatchResult] = {}
        confidence_threshold = get_action_item_confidence_threshold()
        for candidate, segment, quote_timestamps in candidates:
            initial_review_status = (
                ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED
                if candidate.confidence < confidence_threshold
                else ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW
            )
            item = MeetingActionItem(
                id=str(uuid.uuid4()),
                extraction_id=extraction.id,
                owner_name=candidate.owner_name,
                task_description=candidate.task_description,
                deadline_at=candidate.deadline_at,
                deadline_text=candidate.deadline_text,
                priority=candidate.priority,
                confidence=candidate.confidence,
                source_segment_index=candidate.source_segment_index,
                source_start_seconds=segment.start,
                source_end_seconds=segment.end,
                source_quote_start_seconds=(
                    quote_timestamps[0] if quote_timestamps else None
                ),
                source_quote_end_seconds=(
                    quote_timestamps[1] if quote_timestamps else None
                ),
                source_quote=candidate.source_quote.strip(),
                employee_match_status=None,
                ownership_status=MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED,
                review_status=initial_review_status,
                reviewed_by=None,
                reviewed_at=None,
            )
            match_result = _annotate_employee_match(db, item)
            match_results_by_item_id[item.id] = match_result
            db.add(item)
        extraction.status = ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
        extraction.error_message = None
        extraction.completed_at = datetime.now(timezone.utc)
        setattr(extraction, "employee_match_results", match_results_by_item_id)
        db.commit()
        db.refresh(extraction)
        for item in extraction.items:
            match_result = match_results_by_item_id.get(item.id)
            if match_result is None:
                continue
            setattr(item, "employee_match_result", match_result)
            setattr(item, "employee_match_status", match_result.status)
            setattr(item, "proposed_employee_id", match_result.employee_id)
            setattr(item, "proposed_employee_name", match_result.employee_name)
        return extraction

    except Exception as error:
        provider_status = getattr(error, "status_code", None)
        if type(provider_status) is not int:
            provider_status = None
        logger.error(
            "Meeting action item extraction failed transcript_id=%s "
            "exception_type=%s provider_status=%s safe_detail=%s",
            transcript_id,
            type(error).__name__,
            provider_status,
            str(error) if isinstance(error, _SafeExtractionFailure) else None,
        )
        db.rollback()
        failed_extraction = db.get(MeetingActionItemExtraction, extraction.id)
        if failed_extraction is not None:
            failed_extraction.status = ACTION_ITEM_EXTRACTION_STATUS_FAILED
            if isinstance(error, _SafeExtractionFailure):
                failure_message = str(error)
            elif isinstance(error, (json.JSONDecodeError, ValidationError)):
                failure_message = "Groq returned malformed action-item data."
            elif isinstance(error, RuntimeError) and "GROQ_API_KEY" in str(error):
                failure_message = "GROQ_API_KEY is not set."
            else:
                failure_message = "Action item extraction failed."
            failed_extraction.error_message = failure_message
            failed_extraction.completed_at = datetime.now(timezone.utc)
            db.commit()
        raise MeetingActionItemExtractionError(
            "Meeting action item extraction failed."
        ) from None


def review_action_item(
    db: Session,
    meeting_id: str,
    action_item_id: str,
    reviewer_id: str,
    action: str | None = None,
    decision: str | None = None,
    review_status: str | None = None,
    task_description: str | None = None,
    owner_name: str | None = None,
    confirmed_employee_id: str | None = None,
    deadline_at: datetime | None = None,
    deadline_text: str | None = None,
    priority: str | None = None,
) -> MeetingActionItem:
    """Organizer review of an individual action item (confirm, edit, or reject)."""
    item = (
        db.query(MeetingActionItem)
        .join(
            MeetingActionItemExtraction,
            MeetingActionItem.extraction_id == MeetingActionItemExtraction.id,
        )
        .filter(
            MeetingActionItem.id == action_item_id,
            MeetingActionItemExtraction.meeting_id == meeting_id,
        )
        .one_or_none()
    )
    if item is None:
        raise ValueError("Action item not found for this meeting.")
    if item.review_status == ACTION_ITEM_REVIEW_STATUS_REJECTED:
        raise ValueError("Rejected action items cannot be changed.")

    now = datetime.now(timezone.utc)

    # 1. Update confirmed employee if specified
    if confirmed_employee_id is not None:
        if confirmed_employee_id == "":
            item.confirmed_employee_id = None
        else:
            employee = db.get(Employee, confirmed_employee_id)
            if employee is None:
                raise ValueError("Employee not found.")
            if not employee.is_active:
                raise ValueError("Employee is inactive.")
            item.confirmed_employee_id = employee.id
            item.ownership_status = MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED

    # 2. Update editable fields if provided
    if task_description is not None:
        cleaned_desc = task_description.strip()
        if not cleaned_desc:
            raise ValueError("Task description cannot be empty.")
        item.task_description = cleaned_desc

    if owner_name is not None:
        item.owner_name = owner_name.strip() if owner_name.strip() else None

    if deadline_at is not None:
        item.deadline_at = (
            deadline_at.astimezone(timezone.utc)
            if deadline_at.tzinfo is not None
            else deadline_at.replace(tzinfo=timezone.utc)
        )

    if deadline_text is not None:
        item.deadline_text = deadline_text.strip() if deadline_text.strip() else None

    if priority is not None:
        if priority not in ACTION_ITEM_PRIORITIES:
            raise ValueError(f"Priority must be one of {ACTION_ITEM_PRIORITIES}")
        item.priority = priority

    # 3. Determine review decision / status
    target_decision = (action or decision or review_status or "").lower()
    if target_decision in ("reject", "rejected"):
        item.review_status = ACTION_ITEM_REVIEW_STATUS_REJECTED
        item.reviewed_by = reviewer_id
        item.reviewed_at = now
    elif target_decision in ("confirm", "confirmed", "approved"):
        item.review_status = ACTION_ITEM_REVIEW_STATUS_CONFIRMED
        item.reviewed_by = reviewer_id
        item.reviewed_at = now
    elif target_decision in ("edit", "save"):
        item.reviewed_by = reviewer_id
        item.reviewed_at = now
        if review_status in (
            ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
            ACTION_ITEM_REVIEW_STATUS_REJECTED,
            ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
            ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED,
        ):
            item.review_status = review_status
    elif review_status in (
        ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
        ACTION_ITEM_REVIEW_STATUS_REJECTED,
        ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
        ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED,
    ):
        item.review_status = review_status
        item.reviewed_by = reviewer_id
        item.reviewed_at = now
    else:
        item.reviewed_by = reviewer_id
        item.reviewed_at = now

    task_to_notify = None
    if item.review_status == ACTION_ITEM_REVIEW_STATUS_CONFIRMED:
        try:
            task, _ = prepare_task_from_confirmed_action_item(
                db,
                item,
                reviewer_id,
            )
        except ValueError:
            db.rollback()
            raise
        task_to_notify = task

    db.commit()
    if task_to_notify is not None:
        attempt_assignment_email(db, task_to_notify.id)
    db.refresh(item)
    return item


def assert_action_item_eligible_for_task(item: MeetingActionItem) -> None:
    """Story 3.4 safeguard: Ensures rejected or unconfirmed items cannot become tasks."""
    if item.review_status == ACTION_ITEM_REVIEW_STATUS_REJECTED:
        raise ValueError(
            f"Action item {item.id} is rejected and cannot be converted to a task."
        )
    if item.review_status != ACTION_ITEM_REVIEW_STATUS_CONFIRMED:
        raise ValueError(
            f"Action item {item.id} must be confirmed by the meeting organizer before task creation."
        )


def get_task_eligible_action_items(
    db: Session, meeting_id: str
) -> list[MeetingActionItem]:
    """Story 3.4 boundary helper: returns confirmed action items eligible for task creation."""
    return (
        db.query(MeetingActionItem)
        .join(
            MeetingActionItemExtraction,
            MeetingActionItem.extraction_id == MeetingActionItemExtraction.id,
        )
        .filter(
            MeetingActionItemExtraction.meeting_id == meeting_id,
            MeetingActionItem.review_status == ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
        )
        .all()
    )
