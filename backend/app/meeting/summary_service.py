"""Generation and persistence of AI meeting summaries."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.meeting.models import (
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    SUMMARY_STATUS_COMPLETED,
    SUMMARY_STATUS_FAILED,
    SUMMARY_STATUS_PENDING,
    SUMMARY_STATUS_PROCESSING,
    TRANSCRIPT_STATUS_COMPLETED,
)


class SummarySections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: str
    decisions: list[str]
    discussion_topics: list[str]
    open_questions: list[str]


class MeetingSummaryGenerationError(RuntimeError):
    """Raised when a persisted meeting summary cannot be generated."""


def generate_meeting_summary(
    db: Session,
    transcript_id: str,
) -> MeetingSummary:
    transcript = db.get(MeetingTranscript, transcript_id)

    if transcript is None:
        raise ValueError("Meeting transcript not found.")

    if transcript.status != TRANSCRIPT_STATUS_COMPLETED:
        raise ValueError("A completed meeting transcript is required.")

    if not transcript.transcript_text or not transcript.transcript_text.strip():
        raise ValueError("The completed meeting transcript has no text to summarize.")

    summary = (
        db.query(MeetingSummary)
        .filter(MeetingSummary.transcript_id == transcript.id)
        .one_or_none()
    )

    if summary is not None and summary.status == SUMMARY_STATUS_COMPLETED:
        return summary

    if summary is not None and summary.status == SUMMARY_STATUS_PROCESSING:
        return summary

    recording = db.get(MeetingRecording, transcript.recording_id)

    if recording is None:
        raise ValueError("Meeting transcript recording not found.")

    if summary is None:
        summary = MeetingSummary(
            id=str(uuid.uuid4()),
            meeting_id=recording.meeting_id,
            transcript_id=transcript.id,
            status=SUMMARY_STATUS_PENDING,
        )
        db.add(summary)
        db.commit()

    summary.status = SUMMARY_STATUS_PROCESSING
    summary.error_message = None
    summary.completed_at = None
    db.commit()

    try:
        # Load backend/.env
        load_dotenv(
            dotenv_path=Path(__file__).resolve().parents[2] / ".env",
            override=False,
        )

        api_key = os.environ.get("GROQ_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set in the environment or backend/.env."
            )

        from groq import Groq

        client = Groq(api_key=api_key)

        model = os.environ.get(
            "GROQ_SUMMARY_MODEL",
            "openai/gpt-oss-20b",
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize only the supplied persisted meeting transcript. "
                        "Do not invent, infer, or add information not stated in it. "
                        "Return an empty array for any category with no supported "
                        "items. Return exactly these four fields: overview, "
                        "decisions, discussion_topics, and open_questions. "
                        "The response must be valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": transcript.transcript_text,
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content

        if not content:
            raise MeetingSummaryGenerationError(
                "Groq returned no summary."
            )

        parsed = json.loads(content)

        sections = SummarySections.model_validate(parsed)

    except Exception as error:
        summary.status = SUMMARY_STATUS_FAILED
        summary.error_message = str(error)[:4000] or type(error).__name__
        summary.completed_at = datetime.now(timezone.utc)

        db.commit()

        raise MeetingSummaryGenerationError(
            f"Meeting summary generation failed: {summary.error_message}"
        ) from error

    summary.overview = sections.overview
    summary.decisions = sections.decisions
    summary.discussion_topics = sections.discussion_topics
    summary.open_questions = sections.open_questions
    summary.status = SUMMARY_STATUS_COMPLETED
    summary.error_message = None
    summary.completed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(summary)

    return summary