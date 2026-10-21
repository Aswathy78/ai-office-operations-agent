"""Focused tests for Story 6.2 assistant intent understanding."""

from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import AssistantConversation, Base, EmailDraft, Employee
from app.meeting.scheduling_service import resolve_attendees
from app.assistant_understanding import (
    AssistantEntities,
    AssistantUnderstanding,
    explain_understanding,
    understand_assistant_command,
)
import app.assistant_service as assistant_service


REFERENCE_TIME = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


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
    session.add_all([
        Employee(
            id="emp-rahul",
            name="Rahul Sharma",
            email="rahul@northstar.example",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="emp-hr",
            name="Amina Lee",
            email="amina@northstar.example",
            team="HR",
            is_active=True,
        ),
        Employee(
            id="emp-maya",
            name="Maya Thompson",
            email="maya@northstar.example",
            team="Engineering",
            is_active=True,
        ),
    ])
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def understand(db_session: Session, text: str) -> AssistantUnderstanding | None:
    return understand_assistant_command(db_session, text, REFERENCE_TIME)


def test_task_assignment_command_and_variations(db_session: Session):
    for prompt in (
        "Assign the database task to Rahul by Friday.",
        "Please assign database task to Rahul Sharma before Friday.",
        "Could you reassign the database task to Rahul?",
    ):
        result = understand(db_session, prompt)

        assert result is not None
        assert result.intent == "task_assignment"
        assert result.entities.task == "database task"
        assert result.entities.people == ["Rahul Sharma"]
        if "Friday" in prompt:
            assert result.entities.date.isoformat() == "2026-10-23"
        else:
            assert result.status == "understood"


def test_duplicate_first_name_with_single_token_employee_requires_clarification(
    db_session: Session,
):
    db_session.get(Employee, "emp-rahul").is_active = False
    db_session.add_all([
        Employee(
            id="emp-rahul-single",
            name="Rahul",
            email="rahul.single@northstar.example",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="emp-rahul-verma",
            name="Rahul Verma",
            email="rahul.verma@northstar.example",
            team="Design",
            is_active=True,
        ),
    ])
    db_session.commit()

    ambiguous = understand(
        db_session,
        "Schedule a meeting with Rahul tomorrow at 10 AM.",
    )
    explicit = understand(
        db_session,
        "Schedule a meeting with Rahul Verma tomorrow at 10 AM.",
    )
    resolved_attendees, raw_emails, clarification = resolve_attendees(
        db_session,
        "Schedule a meeting with Rahul tomorrow at 10 AM.",
    )

    assert ambiguous is not None
    assert ambiguous.status == "needs_clarification"
    assert ambiguous.entities.people == []
    assert ambiguous.entities.ambiguous_people == {
        "Rahul": ["Rahul", "Rahul Verma"],
    }
    assert explicit is not None
    assert explicit.status == "understood"
    assert explicit.entities.people == ["Rahul Verma"]
    assert explicit.entities.ambiguous_people == {}
    assert resolved_attendees == []
    assert raw_emails == []
    assert clarification is not None
    assert "Rahul" in clarification
    assert "Rahul Verma" in clarification


def test_unique_single_token_employee_name_still_resolves(
    db_session: Session,
):
    db_session.add(Employee(
        id="emp-rhea",
        name="Rhea",
        email="rhea@northstar.example",
        team="Engineering",
        is_active=True,
    ))
    db_session.commit()

    result = understand(
        db_session,
        "Schedule a meeting with Rhea tomorrow at 10 AM.",
    )

    assert result is not None
    assert result.status == "understood"
    assert result.entities.people == ["Rhea"]
    assert result.entities.ambiguous_people == {}


def test_meeting_scheduling_extracts_group_date_time_and_duration(
    db_session: Session,
):
    result = understand(
        db_session,
        "Please schedule a planning meeting with the HR team tomorrow at 2:30 PM "
        "for 1 hour.",
    )

    assert result is not None
    assert result.intent == "meeting_scheduling"
    assert result.status == "understood"
    assert result.entities.groups == ["HR"]
    assert result.entities.meeting == "Planning Meeting"
    assert result.entities.date.isoformat() == "2026-10-22"
    assert result.entities.time.strftime("%H:%M") == "14:30"
    assert result.entities.duration_minutes == 60


def test_meeting_command_without_time_requests_clarification(db_session: Session):
    result = understand(db_session, "Schedule a meeting with the HR team tomorrow.")

    assert result is not None
    assert result.intent == "meeting_scheduling"
    assert result.status == "needs_clarification"
    assert result.entities.groups == ["HR"]
    assert result.entities.date.isoformat() == "2026-10-22"
    assert result.missing_entities == ["time"]
    assert "need time" in explain_understanding(result)


def test_email_command_extracts_recipient_topic_and_date(db_session: Session):
    result = understand(
        db_session,
        "Send an email to the development team about tomorrow's meeting.",
    )

    assert result is not None
    assert result.intent == "send_email"
    assert result.status == "understood"
    assert result.entities.groups == ["Engineering"]
    assert result.entities.topic == "tomorrow's meeting"
    assert result.entities.date.isoformat() == "2026-10-22"


def test_email_deadline_is_separated_from_topic_without_sending(
    db_session: Session,
    monkeypatch,
):
    prompt = "Send an email to Maya about the project update by Friday."
    result = understand(db_session, prompt)

    assert result is not None
    assert result.intent == "send_email"
    assert result.status == "understood"
    assert result.entities.people == ["Maya Thompson"]
    assert result.entities.topic == "the project update"
    assert result.entities.date.isoformat() == "2026-10-23"

    principal = AuthenticatedPrincipal(
        user_id="emp-maya",
        account_id="test-account",
        email="maya@northstar.example",
        name="Maya Thompson",
        role="employee",
        team="Engineering",
        jti="test-token",
    )

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("recognized commands must not reach LLM"),
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/chat/stream",
                json={"message": prompt},
            )
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data:").strip())
            for line in response.text.splitlines()
            if line.startswith("data:") and line.strip() != "data: [DONE]"
        ]
        understanding = events[0]["understanding"]
        assert understanding["intent"] == "send_email"
        assert understanding["status"] == "understood"
        assert understanding["entities"]["people"] == ["Maya Thompson"]
        assert understanding["entities"]["topic"] == "the project update"
        assert understanding["entities"]["date"] == "2026-10-23"
        assert any(
            "I haven't made any changes." in item.get("token", "")
            for item in events
        )
        assert db_session.query(EmailDraft).count() == 0
    finally:
        app.dependency_overrides.clear()


def test_people_and_directory_group_extraction(db_session: Session):
    person_result = understand(
        db_session,
        "Assign the database task to Rahul by Friday.",
    )
    group_result = understand(
        db_session,
        "Send an email to the development team about the release.",
    )

    assert person_result is not None
    assert person_result.entities.people == ["Rahul Sharma"]
    assert group_result is not None
    assert group_result.entities.groups == ["Engineering"]


def test_date_time_and_duration_normalization(db_session: Session):
    result = understand(
        db_session,
        "Schedule a team meeting with HR next Friday at 9 AM for 45 minutes.",
    )

    assert result is not None
    assert result.entities.date.isoformat() == "2026-10-30"
    assert result.entities.time.strftime("%H:%M") == "09:00"
    assert result.entities.duration_minutes == 45


def test_schema_rejects_invalid_time_and_duration():
    with pytest.raises(ValidationError):
        AssistantEntities(time="25:30")

    with pytest.raises(ValidationError):
        AssistantEntities(duration_minutes=1441)


def test_schema_rejects_understood_request_with_missing_entities():
    with pytest.raises(ValidationError):
        AssistantUnderstanding(
            intent="meeting_scheduling",
            status="understood",
            entities=AssistantEntities(groups=["HR"]),
        )


def test_unsupported_action_returns_user_friendly_explanation(db_session: Session):
    result = understand(db_session, "Delete the HR meeting tomorrow.")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.status == "unsupported"
    assert "can't handle that operation yet" in explain_understanding(result)


def test_general_chat_is_left_to_existing_llm_flow(db_session: Session):
    assert understand(db_session, "What can you help me with?") is None


def test_chat_stream_includes_validated_understanding_without_executing_actions(
    db_session: Session,
    monkeypatch,
):
    principal = AuthenticatedPrincipal(
        user_id="emp-rahul",
        account_id="test-account",
        email="rahul@northstar.example",
        name="Rahul Sharma",
        role="employee",
        team="Engineering",
        jti="test-token",
    )

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("recognized commands must not reach LLM"),
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/assistant/chat/stream",
                json={"message": "Assign the database task to Rahul by Friday."},
            )
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data:").strip())
            for line in response.text.splitlines()
            if line.startswith("data:") and line.strip() != "data: [DONE]"
        ]
        metadata = events[0]["understanding"]
        assert metadata["intent"] == "task_assignment"
        assert metadata["entities"]["task"] == "database task"
        assert metadata["entities"]["date"] == "2026-10-23"
        assert any("I haven't made any changes." in item.get("token", "") for item in events)
        assert db_session.query(AssistantConversation).count() == 1
    finally:
        app.dependency_overrides.clear()
