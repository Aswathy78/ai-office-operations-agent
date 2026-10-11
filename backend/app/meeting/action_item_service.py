"""Generation and persistence of timestamped meeting action items."""

from datetime import datetime, timezone
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

from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_EXTRACTION_STATUS_FAILED,
    ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    ACTION_ITEM_EXTRACTION_STATUS_PROCESSING,
    ACTION_ITEM_PRIORITIES,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    TRANSCRIPT_STATUS_COMPLETED,
)
from app.meeting.schemas import TranscriptSegment

logger = logging.getLogger(__name__)


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


def _validate_candidates(
    candidates: ActionItemCandidates,
    segments: list[TranscriptSegment],
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

        if not _has_actionable_intent(quote):
            continue
        quote_timestamps = _find_quote_timestamps(quote, segment)

        if candidate.deadline_text and (
            _normalize_quote(candidate.deadline_text)
            not in _normalize_quote(evidence_text)
        ):
            candidate = candidate.model_copy(update={"deadline_text": None})
        if (
            candidate.deadline_at
            and candidate.deadline_at.date().isoformat() not in evidence_text
        ):
            candidate = candidate.model_copy(update={"deadline_at": None})

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

    extraction = (
        db.query(MeetingActionItemExtraction)
        .filter(MeetingActionItemExtraction.transcript_id == transcript.id)
        .one_or_none()
    )
    if extraction is None:
        extraction = _create_extraction(db, recording.meeting_id, transcript.id)
    if extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED:
            return _refresh_existing_quote_timestamps(db, extraction, transcript)
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
            if not api_key:
                raise RuntimeError("GROQ_API_KEY is not set.")

            from groq import Groq

            client = Groq(api_key=api_key)
            model = os.environ.get(
                "GROQ_ACTION_ITEM_MODEL",
                os.environ.get("GROQ_SUMMARY_MODEL", "openai/gpt-oss-20b"),
            )
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
            candidates = _validate_candidates(parsed_candidates, segments)

        db.query(MeetingActionItem).filter(
            MeetingActionItem.extraction_id == extraction.id
        ).delete(synchronize_session=False)
        for candidate, segment, quote_timestamps in candidates:
            db.add(
                MeetingActionItem(
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
                )
            )
        extraction.status = ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
        extraction.error_message = None
        extraction.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(extraction)
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
