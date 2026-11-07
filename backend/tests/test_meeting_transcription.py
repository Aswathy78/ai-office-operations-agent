import base64
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import database
from app.meeting.models import (
    Base,
    Meeting,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
    TRANSCRIPT_STATUS_PENDING,
    TRANSCRIPT_STATUS_PROCESSING,
)
from app.meeting.recording_encryption import encrypt_recording_file
from app.meeting.service import get_meeting_transcript, update_meeting_transcript
from app.meeting import transcription_worker
from app.meeting.transcription_worker import (
    claim_pending_transcript,
    create_whisper_model,
    process_claimed_transcript,
    run_worker,
)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = testing_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def recording_encryption_key(monkeypatch):
    key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", key)


def _create_completed_recording(
    db_session: Session,
    storage_root,
    media: bytes = b"english meeting audio",
) -> tuple[MeetingRecording, MeetingTranscript]:
    meeting_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="transcription-organizer",
        title="Transcription Test",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=recording_id,
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
        completed_at=now,
    )
    meeting_directory = storage_root / meeting_id
    meeting_directory.mkdir(parents=True, exist_ok=True)
    encrypted_path = meeting_directory / f"{recording_id}.webm"
    plaintext_source = storage_root / f"{recording_id}.source.webm"
    plaintext_source.write_bytes(media)
    encrypt_recording_file(plaintext_source, encrypted_path)
    plaintext_source.unlink()
    recording.storage_uri = f"{meeting_id}/{recording_id}.webm"
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.commit()
    return recording, transcript


def test_transcript_is_unique_per_recording(db_session: Session, tmp_path):
    recording, transcript = _create_completed_recording(db_session, tmp_path)
    duplicate = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
    )
    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()
    assert db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).count() == 1
    assert db_session.get(MeetingTranscript, transcript.id) is not None


def test_get_meeting_transcript_returns_latest_transcript(
    db_session: Session,
    tmp_path,
):
    recording, transcript = _create_completed_recording(db_session, tmp_path)
    second_recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=recording.meeting_id,
        status=RECORDING_STATUS_COMPLETED,
        created_at=transcript.created_at + timedelta(seconds=1),
    )
    latest_transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=second_recording.id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
        created_at=transcript.created_at + timedelta(seconds=1),
    )
    db_session.add_all([second_recording, latest_transcript])
    db_session.commit()

    assert get_meeting_transcript(db_session, recording.meeting_id) == latest_transcript
    assert get_meeting_transcript(db_session, "missing-meeting") is None


def test_update_meeting_transcript_changes_only_segment_text(
    db_session: Session,
    tmp_path,
):
    recording, transcript = _create_completed_recording(db_session, tmp_path)
    original_segments = [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Hello there.",
            "speaker": "Alice",
            "flagged_for_correction": True,
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "How are you?",
            "speaker": "Bob",
            "flagged_for_correction": False,
        },
    ]
    transcript.segments = original_segments
    db_session.commit()

    updated = update_meeting_transcript(
        db_session,
        recording.meeting_id,
        [{"text": "Hi."}, {"text": "Doing well."}],
        "test-editor",
    )

    assert updated is not None
    assert updated.segments == [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Hi.",
            "speaker": "Alice",
            "flagged_for_correction": True,
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "Doing well.",
            "speaker": "Bob",
            "flagged_for_correction": False,
        },
    ]
    assert updated.transcript_text == "Hi. Doing well."
    assert (
        update_meeting_transcript(db_session, "missing-meeting", [], "test-editor")
        is None
    )


def test_worker_claims_pending_transcript_only_once(db_session: Session, tmp_path):
    _, transcript = _create_completed_recording(db_session, tmp_path)

    assert claim_pending_transcript(db_session) == transcript.id
    assert claim_pending_transcript(db_session) is None
    db_session.expire_all()
    assert db_session.get(MeetingTranscript, transcript.id).status == TRANSCRIPT_STATUS_PROCESSING


def test_windows_cuda_dll_directories_are_discovered_and_registered(
    monkeypatch,
    tmp_path,
):
    nvidia_root = tmp_path / "site-packages" / "nvidia"
    expected_directories = tuple(
        nvidia_root / package_name / "bin"
        for package_name in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")
    )
    for directory in expected_directories:
        directory.mkdir(parents=True)
    cublas_library = expected_directories[0] / "cublas64_12.dll"
    cublas_library.touch()

    monkeypatch.setattr(
        transcription_worker,
        "find_spec",
        lambda name: SimpleNamespace(submodule_search_locations=[str(nvidia_root)]),
    )
    dll_handles = []
    library_handles = []

    def add_dll_directory(directory):
        handle = SimpleNamespace(directory=directory)
        dll_handles.append(handle)
        return handle

    def load_dll(library_path):
        handle = SimpleNamespace(library_path=library_path)
        library_handles.append(handle)
        return handle

    monkeypatch.setattr(
        transcription_worker.os,
        "add_dll_directory",
        add_dll_directory,
        raising=False,
    )
    monkeypatch.setattr(transcription_worker.ctypes, "WinDLL", load_dll)
    monkeypatch.setattr(transcription_worker, "_CUDA_DLL_DIRECTORY_HANDLES", [])
    monkeypatch.setattr(transcription_worker, "_CUDA_DLL_LIBRARY_HANDLES", [])
    monkeypatch.setattr(transcription_worker, "_REGISTERED_CUDA_DLL_DIRECTORIES", set())
    monkeypatch.setattr(transcription_worker, "_LOADED_CUDA_DLL_LIBRARIES", set())

    registered = transcription_worker._configure_cuda_dll_directories(windows=True)

    assert registered == tuple(directory.resolve() for directory in expected_directories)
    assert [Path(handle.directory) for handle in dll_handles] == list(registered)
    assert transcription_worker._CUDA_DLL_DIRECTORY_HANDLES == dll_handles
    assert library_handles[0].library_path == str(cublas_library)
    assert transcription_worker._CUDA_DLL_LIBRARY_HANDLES == library_handles
    assert transcription_worker._configure_cuda_dll_directories(windows=True) == ()
    assert len(dll_handles) == len(expected_directories)
    assert len(library_handles) == 1


def test_worker_initialization_registers_and_loads_cuda_before_faster_whisper(
    monkeypatch,
    tmp_path,
):
    nvidia_root = tmp_path / "site-packages" / "nvidia"
    cublas_directory = nvidia_root / "cublas" / "bin"
    cublas_directory.mkdir(parents=True)
    cublas_library = cublas_directory / "cublas64_12.dll"
    cublas_library.touch()
    for package_name in ("cudnn", "cuda_nvrtc", "cuda_runtime"):
        (nvidia_root / package_name / "bin").mkdir(parents=True)

    events = []
    monkeypatch.setattr(
        transcription_worker,
        "find_spec",
        lambda name: SimpleNamespace(submodule_search_locations=[str(nvidia_root)]),
    )
    monkeypatch.setattr(
        transcription_worker.os,
        "add_dll_directory",
        lambda path: events.append(("register", str(Path(path)))) or object(),
        raising=False,
    )
    monkeypatch.setattr(
        transcription_worker.ctypes,
        "WinDLL",
        lambda path: events.append(("load", Path(path).name)) or object(),
    )
    monkeypatch.setattr(transcription_worker, "_CUDA_DLL_DIRECTORY_HANDLES", [])
    monkeypatch.setattr(transcription_worker, "_CUDA_DLL_LIBRARY_HANDLES", [])
    monkeypatch.setattr(transcription_worker, "_REGISTERED_CUDA_DLL_DIRECTORIES", set())
    monkeypatch.setattr(transcription_worker, "_LOADED_CUDA_DLL_LIBRARIES", set())

    class FakeWhisperModel:
        def __init__(self, model_name, **options):
            events.append(("construct", model_name, options))

    fake_faster_whisper = SimpleNamespace(WhisperModel=FakeWhisperModel)
    fake_ctranslate2 = SimpleNamespace()
    original_import = __import__

    def import_with_order_check(name, *args, **kwargs):
        if name in {"faster_whisper", "ctranslate2"}:
            events.append(("import", name))
            assert all(
                ("register", str(directory.resolve())) in events
                for directory in (
                    cublas_directory,
                    nvidia_root / "cudnn" / "bin",
                    nvidia_root / "cuda_nvrtc" / "bin",
                    nvidia_root / "cuda_runtime" / "bin",
                )
            )
            assert ("load", "cublas64_12.dll") in events
            assert not any(event[0] == "construct" for event in events)
            if name == "ctranslate2":
                return fake_ctranslate2
            import_with_order_check("ctranslate2")
            return fake_faster_whisper
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", import_with_order_check)

    create_whisper_model()

    assert [event[0] for event in events] == [
        "register",
        "register",
        "register",
        "register",
        "load",
        "import",
        "import",
        "construct",
    ]
    assert events[5:7] == [("import", "faster_whisper"), ("import", "ctranslate2")]
    assert events[-1] == (
        "construct",
        "small",
        {"device": "cuda", "compute_type": "float16"},
    )


def test_create_whisper_model_uses_cuda_small_float16(monkeypatch, caplog):
    constructor_calls = []
    setup_calls = []

    class FakeWhisperModel:
        def __init__(self, model_name, **options):
            setup_calls.append("model")
            constructor_calls.append((model_name, options))

    monkeypatch.setattr(
        transcription_worker,
        "_configure_cuda_dll_directories",
        lambda: setup_calls.append("dll setup") or (),
    )
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeWhisperModel),
    )

    with caplog.at_level("INFO"):
        model = create_whisper_model()

    assert isinstance(model, FakeWhisperModel)
    assert constructor_calls == [
        ("small", {"device": "cuda", "compute_type": "float16"})
    ]
    assert setup_calls == ["dll setup", "model"]
    assert "Whisper model loading" in caplog.text
    assert "Whisper model loaded" in caplog.text
    assert "device=cuda" in caplog.text
    assert "compute_type=float16" in caplog.text


def test_create_whisper_model_surfaces_cuda_initialization_error(
    monkeypatch,
    caplog,
):
    constructor_calls = []

    def fail_initialization(*args, **kwargs):
        constructor_calls.append((args, kwargs))
        raise RuntimeError("CUDA driver initialization failed")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=fail_initialization),
    )

    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError,
        match="CUDA driver initialization failed",
    ):
        create_whisper_model()

    assert "device=cuda" in caplog.text
    assert "compute_type=float16" in caplog.text
    assert "Whisper model loaded" not in caplog.text
    assert constructor_calls == [
        (("small",), {"device": "cuda", "compute_type": "float16"})
    ]


def test_worker_logs_startup(caplog):
    stop_event = threading.Event()
    stop_event.set()
    with caplog.at_level("INFO"):
        run_worker(stop_event=stop_event)

    assert "Transcription worker started" in caplog.text


def test_worker_uses_database_path_independent_of_working_directory(
    monkeypatch,
    tmp_path,
):
    # The default location is fixed next to the project, never relative to where the
    # process was started (the test suite itself points the path at a temporary file).
    default_database = (
        Path(database.__file__).resolve().parents[3] / "office_operations.db"
    )
    assert database.DEFAULT_DATABASE_PATH == default_database
    assert default_database.is_absolute()
    expected_database = database.DATABASE_PATH
    monkeypatch.chdir(tmp_path)

    assert Path(database.engine.url.database) == expected_database
    assert expected_database.is_absolute()
    assert transcription_worker.SessionLocal is database.SessionLocal


def test_worker_logs_when_polling_without_pending_transcripts(
    db_session: Session,
    caplog,
):
    stop_event = threading.Event()
    session_factory = sessionmaker(bind=db_session.get_bind())

    def stop_after_one_poll():
        stop_event.set()
        return session_factory()

    with caplog.at_level("INFO", logger=transcription_worker.__name__):
        run_worker(
            poll_interval_seconds=0,
            session_factory=stop_after_one_poll,
            stop_event=stop_event,
        )

    assert "poll_interval_seconds=0" in caplog.text
    assert "No pending transcripts" in caplog.text


def test_worker_loads_dotenv_before_decrypting_recording(
    db_session: Session,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    _, transcript = _create_completed_recording(db_session, storage_root)
    expected_key = os.environ["RECORDING_ENCRYPTION_KEY"]
    monkeypatch.delenv("RECORDING_ENCRYPTION_KEY")

    dotenv_calls: list[tuple[Path, bool]] = []
    decryption_calls: list[str] = []
    stop_event = threading.Event()
    real_decrypt = transcription_worker.decrypt_recording_to_temporary_file

    def load_worker_environment(*, dotenv_path, override):
        dotenv_calls.append((Path(dotenv_path), override))
        monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", expected_key)

    def decrypt_after_configuration(encrypted_path, *, temporary_directory=None):
        assert dotenv_calls
        assert Path(encrypted_path).is_file()
        result = real_decrypt(
            encrypted_path,
            temporary_directory=temporary_directory,
        )
        decryption_calls.append(str(encrypted_path))
        stop_event.set()
        return result

    class FakeWhisperModel:
        def transcribe(self, _audio_path, *, language, word_timestamps):
            assert language == "en"
            assert word_timestamps is True
            spoken = SimpleNamespace(start=0.0, end=1.0, text="Hello team.", words=[])
            return iter([spoken]), SimpleNamespace(language="en")

    monkeypatch.setattr(transcription_worker, "load_dotenv", load_worker_environment)
    monkeypatch.setattr(
        transcription_worker,
        "decrypt_recording_to_temporary_file",
        decrypt_after_configuration,
    )

    run_worker(
        poll_interval_seconds=0,
        session_factory=sessionmaker(bind=db_session.get_bind()),
        model_factory=FakeWhisperModel,
        storage_root=storage_root,
        stop_event=stop_event,
    )

    assert dotenv_calls == [
        (Path(transcription_worker.__file__).resolve().parents[2] / ".env", False)
    ]
    assert len(decryption_calls) == 1
    db_session.expire_all()
    assert db_session.get(MeetingTranscript, transcript.id).status == TRANSCRIPT_STATUS_COMPLETED


def test_worker_persists_cuda_initialization_failure(
    db_session: Session,
    tmp_path,
    caplog,
):
    _, transcript = _create_completed_recording(db_session, tmp_path)
    stop_event = threading.Event()

    def fail_model_initialization():
        stop_event.set()
        raise RuntimeError("CUDA driver initialization failed")

    with caplog.at_level("ERROR"):
        run_worker(
            session_factory=sessionmaker(bind=db_session.get_bind()),
            model_factory=fail_model_initialization,
            storage_root=tmp_path,
            stop_event=stop_event,
        )

    db_session.expire_all()
    failed = db_session.get(MeetingTranscript, transcript.id)
    assert failed.status == TRANSCRIPT_STATUS_FAILED
    assert failed.error_message == "RuntimeError: CUDA driver initialization failed"
    assert "CUDA driver initialization failed" in caplog.text


def test_worker_stores_english_timestamped_segments_and_cleans_plaintext(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    temporary_directory = tmp_path / "plaintext"
    temporary_directory.mkdir()
    recording, transcript = _create_completed_recording(
        db_session,
        storage_root,
        media=b"decoded english audio",
    )
    claimed_id = claim_pending_transcript(db_session)
    assert claimed_id == transcript.id

    class FakeWhisperModel:
        language = None
        input_path = None

        def transcribe(self, audio_path, *, language, word_timestamps):
            self.language = language
            self.word_timestamps = word_timestamps
            self.input_path = audio_path
            assert Path(audio_path).read_bytes() == b"decoded english audio"
            return iter(
                [
                    SimpleNamespace(
                        start=0.25,
                        end=1.5,
                        text=" Hello there.",
                        words=[
                            SimpleNamespace(start=0.3, end=0.7, word=" Hello"),
                            SimpleNamespace(start=0.7, end=1.2, word=" there."),
                        ],
                    ),
                    SimpleNamespace(
                        start=1.5,
                        end=2.75,
                        text=" How are you?",
                        words=[
                            SimpleNamespace(start=1.6, end=1.8, word=" How"),
                            SimpleNamespace(start=1.8, end=2.0, word=" are"),
                            SimpleNamespace(start=2.0, end=2.5, word=" you?"),
                        ],
                    ),
                ]
            ), SimpleNamespace(language="en")

    model = FakeWhisperModel()
    assert process_claimed_transcript(
        db_session,
        transcript.id,
        model,
        storage_root=storage_root,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    completed = db_session.get(MeetingTranscript, transcript.id)
    assert completed.status == TRANSCRIPT_STATUS_COMPLETED
    assert completed.language == "en"
    assert completed.transcript_text == "Hello there. How are you?"
    assert completed.segments == [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Hello there.",
            "speaker": "Unknown Speaker",
            "flagged_for_correction": True,
            "words": [
                {"start": 0.3, "end": 0.7, "word": " Hello"},
                {"start": 0.7, "end": 1.2, "word": " there."},
            ],
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "How are you?",
            "speaker": "Unknown Speaker",
            "flagged_for_correction": True,
            "words": [
                {"start": 1.6, "end": 1.8, "word": " How"},
                {"start": 1.8, "end": 2.0, "word": " are"},
                {"start": 2.0, "end": 2.5, "word": " you?"},
            ],
        },
    ]
    assert model.language == "en"
    assert model.word_timestamps is True
    assert not Path(model.input_path).exists()
    assert list(temporary_directory.iterdir()) == []
    assert db_session.get(MeetingRecording, recording.id).status == RECORDING_STATUS_COMPLETED


def test_worker_persists_transcript_for_workflow_processing(
    db_session: Session,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    recording, transcript = _create_completed_recording(
        db_session,
        storage_root,
        media=b"meeting audio",
    )
    assert claim_pending_transcript(db_session) == transcript.id
    expected_segments = [
        {
            "start": 0.5,
            "end": 2.25,
            "text": "Maya, please send the agenda.",
            "speaker": "Unknown Speaker",
            "flagged_for_correction": True,
        }
    ]

    class FakeWhisperModel:
        def transcribe(self, audio_path, *, language, word_timestamps):
            return iter(
                [SimpleNamespace(start=0.5, end=2.25, text=" Maya, please send the agenda.")]
            ), SimpleNamespace(language="en")

    assert process_claimed_transcript(
        db_session,
        transcript.id,
        FakeWhisperModel(),
        storage_root=storage_root,
    )

    db_session.expire_all()
    completed = db_session.get(MeetingTranscript, transcript.id)
    assert completed.status == TRANSCRIPT_STATUS_COMPLETED
    assert completed.segments == expected_segments
    assert (
        db_session.query(MeetingSummary)
        .filter(MeetingSummary.transcript_id == transcript.id)
        .one_or_none()
        is None
    )
    assert (
        db_session.query(MeetingActionItemExtraction)
        .filter(MeetingActionItemExtraction.transcript_id == transcript.id)
        .one_or_none()
        is None
    )
    assert db_session.get(MeetingRecording, recording.id).status == RECORDING_STATUS_COMPLETED


def test_transcription_does_not_change_existing_summary(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    recording, transcript = _create_completed_recording(db_session, storage_root)
    existing_summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=recording.meeting_id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_COMPLETED,
        overview="Persisted summary must remain unchanged.",
        decisions=["Keep the launch date."],
        discussion_topics=["Timeline"],
        open_questions=[],
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(existing_summary)
    db_session.commit()
    assert claim_pending_transcript(db_session) == transcript.id

    class FakeWhisperModel:
        def transcribe(self, audio_path, *, language, word_timestamps):
            return iter(
                [SimpleNamespace(start=3.0, end=4.0, text="I will send the notes.")]
            ), SimpleNamespace(language="en")

    assert process_claimed_transcript(
        db_session,
        transcript.id,
        FakeWhisperModel(),
        storage_root=storage_root,
    )

    db_session.expire_all()
    completed = db_session.get(MeetingTranscript, transcript.id)
    persisted_summary = db_session.get(MeetingSummary, existing_summary.id)
    assert completed.status == TRANSCRIPT_STATUS_COMPLETED
    assert completed.segments[0]["start"] == 3.0
    assert completed.segments[0]["end"] == 4.0
    assert persisted_summary.status == SUMMARY_STATUS_COMPLETED
    assert persisted_summary.overview == "Persisted summary must remain unchanged."
    assert persisted_summary.decisions == ["Keep the launch date."]


def test_decryption_failure_is_persisted_and_plaintext_is_removed(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    temporary_directory = tmp_path / "plaintext"
    temporary_directory.mkdir()
    recording, transcript = _create_completed_recording(db_session, storage_root)
    encrypted_path = storage_root / recording.storage_uri
    encrypted_path.write_bytes(b"not an encrypted recording")
    assert claim_pending_transcript(db_session) == transcript.id

    assert not process_claimed_transcript(
        db_session,
        transcript.id,
        model=object(),
        storage_root=storage_root,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    failed = db_session.get(MeetingTranscript, transcript.id)
    assert failed.status == TRANSCRIPT_STATUS_FAILED
    assert failed.error_message
    assert failed.completed_at is not None
    assert list(temporary_directory.iterdir()) == []


def test_inference_failure_is_persisted_and_plaintext_is_removed(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    temporary_directory = tmp_path / "plaintext"
    temporary_directory.mkdir()
    _, transcript = _create_completed_recording(db_session, storage_root)
    assert claim_pending_transcript(db_session) == transcript.id

    class FailingWhisperModel:
        input_path = None

        def transcribe(self, audio_path, *, language, word_timestamps):
            self.input_path = audio_path
            raise RuntimeError("inference unavailable")

    model = FailingWhisperModel()
    assert not process_claimed_transcript(
        db_session,
        transcript.id,
        model,
        storage_root=storage_root,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    failed = db_session.get(MeetingTranscript, transcript.id)
    assert failed.status == TRANSCRIPT_STATUS_FAILED
    assert "inference unavailable" in failed.error_message
    assert failed.completed_at is not None
    assert not Path(model.input_path).exists()
    assert list(temporary_directory.iterdir()) == []