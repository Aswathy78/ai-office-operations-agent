"""Regression tests for transcripts that "randomly" never finished.

* Stuck "processing" rows (worker stopped or crashed mid-job) are re-queued.
* A failing claim (e.g. "database is locked") no longer kills the worker loop.
* The API starts the worker itself, and says clearly when it cannot.
"""

from datetime import datetime, timedelta, timezone
import threading

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.meeting.transcription_worker as worker
from app.meeting.models import (
    Base,
    MeetingTranscript,
    TRANSCRIPT_STATUS_PENDING,
    TRANSCRIPT_STATUS_PROCESSING,
)


def _session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _transcript(transcript_id: str, status: str, started_minutes_ago: float | None):
    started = (
        datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
        if started_minutes_ago is not None
        else None
    )
    return MeetingTranscript(
        id=transcript_id, recording_id=f"recording-{transcript_id}", status=status,
        language="en", processing_started_at=started,
    )


def test_stale_processing_rows_are_requeued_but_active_ones_are_not():
    Session = _session_factory()
    with Session() as db:
        db.add_all([
            _transcript("stuck", TRANSCRIPT_STATUS_PROCESSING, 120),
            _transcript("active", TRANSCRIPT_STATUS_PROCESSING, 2),
            _transcript("waiting", TRANSCRIPT_STATUS_PENDING, None),
        ])
        db.commit()
        assert worker.requeue_stale_transcripts(db, older_than_minutes=30) == 1
        db.expire_all()
        assert db.get(MeetingTranscript, "stuck").status == TRANSCRIPT_STATUS_PENDING
        assert db.get(MeetingTranscript, "stuck").processing_started_at is None
        assert db.get(MeetingTranscript, "active").status == TRANSCRIPT_STATUS_PROCESSING
        # The re-queued job is claimed normally afterwards.
        assert worker.claim_pending_transcript(db) == "stuck"


def test_worker_survives_a_failing_claim(monkeypatch):
    Session = _session_factory()
    stop = threading.Event()
    calls = {"claims": 0}

    def flaky_claim(db):
        calls["claims"] += 1
        if calls["claims"] == 1:
            raise OperationalError("UPDATE meeting_transcripts", {}, Exception("database is locked"))
        stop.set()
        return None

    monkeypatch.setattr(worker, "claim_pending_transcript", flaky_claim)
    worker.run_worker(
        poll_interval_seconds=0.01,
        session_factory=Session,
        model_factory=lambda: None,
        stop_event=stop,
    )
    # Before the fix the first error propagated and the worker exited.
    assert calls["claims"] == 2


def test_failure_recording_error_does_not_kill_the_worker(monkeypatch):
    Session = _session_factory()
    with Session() as db:
        db.add(_transcript("job", TRANSCRIPT_STATUS_PENDING, None))
        db.commit()
    stop = threading.Event()

    def broken_model():
        stop.set()
        raise RuntimeError("model download failed")

    def broken_persist(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(worker, "_persist_failure", broken_persist)
    worker.run_worker(
        poll_interval_seconds=0.01, session_factory=Session,
        model_factory=broken_model, stop_event=stop,
    )
    with Session() as db:
        # Left in processing, where requeue_stale_transcripts will pick it up later.
        assert db.get(MeetingTranscript, "job").status == TRANSCRIPT_STATUS_PROCESSING


def test_background_worker_respects_the_switch_and_missing_whisper(monkeypatch, caplog):
    monkeypatch.setenv("TRANSCRIPTION_WORKER_ENABLED", "false")
    assert worker.start_transcription_worker() is False

    monkeypatch.setenv("TRANSCRIPTION_WORKER_ENABLED", "true")
    monkeypatch.setattr(worker, "find_spec", lambda name: None)
    with caplog.at_level("WARNING"):
        assert worker.start_transcription_worker() is False
    assert "faster-whisper is not installed" in caplog.text


def test_background_worker_runs_and_stops(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_WORKER_ENABLED", "true")
    monkeypatch.setattr(worker, "find_spec", lambda name: object())
    started = threading.Event()

    def fake_run_worker(*, stop_event, **kwargs):
        started.set()
        stop_event.wait(5)

    monkeypatch.setattr(worker, "run_worker", fake_run_worker)
    try:
        assert worker.start_transcription_worker() is True
        assert started.wait(2)
    finally:
        worker.stop_transcription_worker()
    assert worker._background_thread is None
