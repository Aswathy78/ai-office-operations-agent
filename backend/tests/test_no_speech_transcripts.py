"""A recording with noise but no speech must fail clearly, not become an invented transcript.

Whisper turns room noise into fluent captions ("Thank you so much for watching this
video and I'll see you in the next one."). That reached the meeting page as if it had
been said, the follow-up workflow "completed", and nobody was told anything was wrong.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import inspect
import io
import math
import secrets
import struct
import uuid
import wave

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting.models import (
    AuthAccount,
    Base,
    Employee,
    Meeting,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
    TRANSCRIPT_STATUS_PENDING,
    UserNotification,
    WorkflowDeadLetter,
    WorkflowRun,
)
from app.meeting.recording_encryption import encrypt_recording_file
from app.meeting.transcription_worker import (
    NO_SPEECH_MESSAGE,
    SPEECH_ONLY_OPTIONS,
    claim_pending_transcript,
    looks_invented,
    process_claimed_transcript,
    supported_options,
)
from app.workflow_service import claim_workflow, enqueue_meeting_workflow
from app.workflow_worker import process_workflow

# What Whisper actually produced for a recording nobody spoke in.
INVENTED = "Thank you so much for watching this video and I'll see you in the next one."


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
    monkeypatch.setattr("app.notification_service.deliver_notification", lambda *a, **k: None)


def _tone(seconds: float = 3.0, amplitude: float = 0.06) -> bytes:
    """Audible but not speech: a steady tone, so the plain silence check lets it through."""
    rate = 16000
    frames = b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(int(seconds * rate))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


def _recorded_meeting(db: Session, storage_root, *, title: str = "no speech") -> MeetingTranscript:
    meeting_id, recording_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(Meeting(id=meeting_id, organizer_id="organizer", title=title, start_at=now - timedelta(hours=1),
                   end_at=now, join_url=f"http://localhost/meetings/{meeting_id}", status="completed"))
    relative = f"{meeting_id}/{recording_id}.webm"
    path = storage_root / relative
    path.parent.mkdir(parents=True)
    plaintext = path.with_suffix(".plain")
    plaintext.write_bytes(_tone())
    encrypt_recording_file(plaintext, path)
    plaintext.unlink()
    db.add(MeetingRecording(id=recording_id, meeting_id=meeting_id, status=RECORDING_STATUS_COMPLETED,
                            storage_uri=relative, completed_at=now))
    transcript = MeetingTranscript(id=str(uuid.uuid4()), recording_id=recording_id,
                                   status=TRANSCRIPT_STATUS_PENDING)
    db.add(transcript)
    db.commit()
    return transcript


def segment(text, *, start=0.0, end=2.0, no_speech=0.0, logprob=-0.2):
    return type("Segment", (), {"start": start, "end": end, "text": text, "words": [],
                                "no_speech_prob": no_speech, "avg_logprob": logprob})()


class Model:
    """A fake Whisper that returns the given segments and records what it was asked."""

    def __init__(self, *segments):
        self.segments = segments
        self.options: dict = {}

    def transcribe(self, audio_path, *, language, word_timestamps, **options):
        self.options = options
        return iter(self.segments), None


def _transcribe(db: Session, tmp_path, model) -> MeetingTranscript:
    root = tmp_path / "recordings"
    root.mkdir()
    transcript = _recorded_meeting(db, root)
    assert claim_pending_transcript(db) == transcript.id
    process_claimed_transcript(db, transcript.id, model, storage_root=root, temporary_directory=tmp_path)
    db.expire_all()
    return db.get(MeetingTranscript, transcript.id)


# ---------------------------------------------------------------------------
# Telling invented text from speech
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "no_speech", "logprob", "invented"), [
    (INVENTED, 0.85, -0.9, True),                         # the line from the meeting page
    ("You", 0.88, -0.91, True),                            # what noise usually becomes
    ("Thanks for watching!", 0.4, -0.5, True),
    ("Please subscribe to my channel.", 0.5, -0.4, True),
    ("Bye.", 0.35, -0.6, True),
    ("We should ship the report by Friday.", 0.7, -0.5, True),   # Whisper itself says: silence
    ("", 0.0, 0.0, True),
    ("We should ship the report by Friday.", 0.02, -0.2, False),
    ("Thank you.", 0.02, -0.2, False),                     # a person really can say this
    (INVENTED, 0.05, -0.3, False),                         # said confidently, so it is kept
    ("You need to approve the budget.", 0.05, -0.3, False),
    ("Subscribe the team to the alerts channel.", 0.05, -0.3, False),
])
def test_invented_text_is_told_apart_from_real_speech(text, no_speech, logprob, invented):
    assert looks_invented(text, no_speech, logprob) is invented


def test_a_segment_without_confidence_figures_is_never_dropped_for_them():
    # Older callers and test doubles give only text and times.
    assert looks_invented("You", 0.0, 0.0) is False


# ---------------------------------------------------------------------------
# Asking Whisper to skip non-speech
# ---------------------------------------------------------------------------


def test_the_speech_only_options_exist_in_the_installed_whisper():
    faster_whisper = pytest.importorskip("faster_whisper")
    accepted = inspect.signature(faster_whisper.WhisperModel.transcribe).parameters
    assert set(SPEECH_ONLY_OPTIONS) <= set(accepted)
    assert SPEECH_ONLY_OPTIONS["vad_filter"] is True


def test_options_are_passed_only_to_models_that_accept_them():
    class Plain:
        def transcribe(self, audio_path, *, language, word_timestamps): ...

    class Open:
        def transcribe(self, audio_path, *, language, word_timestamps, **options): ...

    class Partial:
        def transcribe(self, audio_path, *, language, word_timestamps, vad_filter=False): ...

    assert supported_options(Plain(), SPEECH_ONLY_OPTIONS) == {}
    assert supported_options(Open(), SPEECH_ONLY_OPTIONS) == SPEECH_ONLY_OPTIONS
    assert supported_options(Partial(), SPEECH_ONLY_OPTIONS) == {"vad_filter": True}
    assert supported_options(object(), SPEECH_ONLY_OPTIONS) == {}


def test_the_worker_asks_whisper_to_skip_non_speech(db_session, tmp_path):
    model = Model(segment("Let us begin."))

    _transcribe(db_session, tmp_path, model)

    assert model.options["vad_filter"] is True
    assert model.options["condition_on_previous_text"] is False


# ---------------------------------------------------------------------------
# What happens to the recording
# ---------------------------------------------------------------------------


def test_a_recording_where_nobody_speaks_fails_clearly_instead_of_inventing_a_transcript(db_session, tmp_path):
    transcript = _transcribe(db_session, tmp_path, Model(segment(INVENTED, no_speech=0.85, logprob=-0.9)))

    assert transcript.status == TRANSCRIPT_STATUS_FAILED
    assert NO_SPEECH_MESSAGE in transcript.error_message
    assert "microphone" in transcript.error_message
    assert transcript.transcript_text is None and not transcript.segments
    assert INVENTED not in (transcript.error_message or "")


def test_a_recording_with_no_segments_at_all_is_also_no_speech(db_session, tmp_path):
    transcript = _transcribe(db_session, tmp_path, Model())

    assert transcript.status == TRANSCRIPT_STATUS_FAILED
    assert NO_SPEECH_MESSAGE in transcript.error_message


def test_an_invented_line_after_real_speech_is_dropped_and_the_speech_kept(db_session, tmp_path):
    model = Model(
        segment("Let us review the budget.", start=0.0, end=2.5, no_speech=0.01),
        segment("You", start=12.2, end=13.6, no_speech=0.82, logprob=-0.95),
    )

    transcript = _transcribe(db_session, tmp_path, model)

    assert transcript.status == TRANSCRIPT_STATUS_COMPLETED
    assert transcript.transcript_text == "Let us review the budget."
    assert [s["text"] for s in transcript.segments] == ["Let us review the budget."]


def test_a_spoken_thank_you_is_kept(db_session, tmp_path):
    transcript = _transcribe(db_session, tmp_path, Model(segment("Thank you.", no_speech=0.02)))

    assert transcript.status == TRANSCRIPT_STATUS_COMPLETED
    assert transcript.transcript_text == "Thank you."


def test_the_failure_is_shown_to_people_without_the_technical_prefix(db_session, tmp_path):
    from app.meeting.schemas import MeetingTranscriptResponse  # noqa: F401  (imported to prove it exists)

    transcript = _transcribe(db_session, tmp_path, Model())

    assert transcript.error_message.startswith("NoSpeechDetectedError:")  # stored for troubleshooting
    from app.plain_errors import plain_error

    assert plain_error(transcript.error_message) == NO_SPEECH_MESSAGE


# ---------------------------------------------------------------------------
# The follow-up workflow and who is told
# ---------------------------------------------------------------------------


def _person(db: Session, employee_id: str, role: str) -> None:
    db.add(Employee(id=employee_id, name=f"Person {employee_id}", email=f"{employee_id}@example.test",
                    is_active=True))
    db.add(AuthAccount(id=f"acc-{employee_id}", employee_id=employee_id,
                       email_normalized=f"{employee_id}@example.test", password_hash="x",
                       role=role, is_enabled=True))
    db.commit()


def test_a_no_speech_meeting_fails_its_workflow_at_once_and_tells_the_organizer_and_the_administrator(
    db_session, tmp_path,
):
    _person(db_session, "organizer", "manager")
    _person(db_session, "admin-1", "admin")
    transcript = _transcribe(db_session, tmp_path, Model(segment(INVENTED, no_speech=0.9, logprob=-0.9)))
    recording = db_session.get(MeetingRecording, transcript.recording_id)
    run = enqueue_meeting_workflow(db_session, recording.meeting_id)
    assert run is not None

    claimed = claim_workflow(db_session)
    process_workflow(db_session, claimed.id)
    db_session.expire_all()

    run = db_session.get(WorkflowRun, run.id)
    assert run.status == "failed"
    assert run.attempt_count == 1, "retrying cannot make people speak, so it fails at once"
    assert db_session.query(MeetingSummary).count() == 0
    [entry] = db_session.query(WorkflowDeadLetter).all()
    assert entry.failed_step_key == "transcription" and entry.status == "open"
    assert NO_SPEECH_MESSAGE == entry.error_message

    inbox = {n.recipient_employee_id: n for n in db_session.query(UserNotification)}
    assert set(inbox) == {"organizer", "admin-1"}
    assert inbox["organizer"].event_type == "meeting_workflow_failed"
    assert "failed for no speech" in inbox["organizer"].summary.lower()
    assert "No speech was detected" in inbox["organizer"].summary
    assert inbox["admin-1"].event_type == "workflow_failure"
    assert "No speech was detected" in inbox["admin-1"].summary
    assert inbox["admin-1"].entity_url == "/admin/workflows"


def test_a_meeting_with_real_speech_still_completes_its_transcript_step(db_session, tmp_path):
    _person(db_session, "organizer", "manager")
    _person(db_session, "admin-1", "admin")
    transcript = _transcribe(db_session, tmp_path, Model(segment("Let us review the budget.")))

    assert transcript.status == TRANSCRIPT_STATUS_COMPLETED
    assert db_session.query(UserNotification).count() == 0, "nothing to report when it works"
