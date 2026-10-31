"""Tests for AI Assistant Streaming Chat, Persistence, and Security (Epic 6.1 / FR-30)."""

from datetime import datetime, timezone
import json
import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.models import (
    AssistantConversation,
    AssistantMessage,
    Base,
    Employee,
    Meeting,
    MeetingParticipant,
    Task,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
import app.assistant_service as assistant_service


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]


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


@pytest.fixture()
def employee_a(db_session: Session):
    emp = Employee(
        id="emp-1",
        name="Maya Thompson",
        email="maya@northstar.example",
        team="Engineering",
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture()
def employee_b(db_session: Session):
    emp = Employee(
        id="emp-2",
        name="Ava Patel",
        email="ava.patel@northstar.example",
        team="Design",
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture()
def principal_a(employee_a: Employee):
    return AuthenticatedPrincipal(
        user_id=employee_a.id,
        account_id="acc-1",
        email=employee_a.email,
        name=employee_a.name,
        role="employee",
        team=employee_a.team,
        jti="jti-token-a",
    )


@pytest.fixture()
def principal_b(employee_b: Employee):
    return AuthenticatedPrincipal(
        user_id=employee_b.id,
        account_id="acc-2",
        email=employee_b.email,
        name=employee_b.name,
        role="employee",
        team=employee_b.team,
        jti="jti-token-b",
    )


@pytest.fixture()
def client(db_session: Session, principal_a: AuthenticatedPrincipal):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = lambda: principal_a
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_unauthenticated_requests_are_rejected(db_session: Session):
    """Verify that unauthenticated requests to chat endpoints return HTTP 401."""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = lambda: db_session

    with TestClient(app) as unauth_client:
        res = unauth_client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Unauthenticated test"},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "Authentication is required."

        conv_res = unauth_client.get("/api/v1/assistant/conversations")
        assert conv_res.status_code == 401

    app.dependency_overrides.clear()


def test_cross_user_conversation_isolation_and_authorization(
    client: TestClient,
    db_session: Session,
    principal_a: AuthenticatedPrincipal,
    principal_b: AuthenticatedPrincipal,
    monkeypatch,
):
    """Verify that employee B cannot read, append to, or delete employee A's conversation."""
    def mock_stream(messages, temperature=0.3):
        yield "Response for Maya"

    monkeypatch.setattr(assistant_service, "stream_chat_completion", mock_stream)

    # Employee A creates conversation
    app.dependency_overrides[require_authenticated_principal] = lambda: principal_a
    res = client.post("/api/v1/assistant/chat/stream", json={"message": "Maya private note"})
    assert res.status_code == 200
    first_line = [l for l in res.text.split("\n") if l.startswith("data:")][0]
    conv_a = json.loads(first_line.replace("data:", "").strip())["conversation_id"]

    # Switch identity to Employee B
    app.dependency_overrides[require_authenticated_principal] = lambda: principal_b

    # Employee B tries to append to Employee A's conversation -> 403 Forbidden
    hack_post = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Injected by Ava", "conversation_id": conv_a},
    )
    assert hack_post.status_code == 403
    assert hack_post.json()["detail"] == "Access to this conversation is denied."

    # Employee B tries to read Employee A's conversation -> 403 Forbidden
    hack_get = client.get(f"/api/v1/assistant/conversations/{conv_a}")
    assert hack_get.status_code == 403
    assert hack_get.json()["detail"] == "Access to this conversation is denied."

    # Employee B tries to delete Employee A's conversation -> 403 Forbidden
    hack_del = client.delete(f"/api/v1/assistant/conversations/{conv_a}")
    assert hack_del.status_code == 403
    assert hack_del.json()["detail"] == "Access to this conversation is denied."

    # Restore identity to Employee A
    app.dependency_overrides[require_authenticated_principal] = lambda: principal_a


def test_streaming_chat_returns_sse_and_incremental_chunks(client: TestClient, monkeypatch):
    """Verify that /api/v1/assistant/chat/stream returns text/event-stream with progressive tokens."""
    tokens_to_yield = ["Hello", " Maya", ", I", " am", " your", " operations", " assistant."]

    def mock_stream(messages, temperature=0.3):
        for token in tokens_to_yield:
            yield token

    monkeypatch.setattr(assistant_service, "stream_chat_completion", mock_stream)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Hello assistant!"},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    lines = [line.strip() for line in response.text.split("\n") if line.strip()]
    assert len(lines) >= 3

    first_event = lines[0]
    assert first_event.startswith("data:")
    parsed_first = json.loads(first_event.replace("data:", "").strip())
    assert "conversation_id" in parsed_first
    conv_id = parsed_first["conversation_id"]

    received_tokens = []
    for line in lines[1:]:
        if line == "data: [DONE]":
            break
        data = json.loads(line.replace("data:", "").strip())
        if "token" in data:
            received_tokens.append(data["token"])

    assert received_tokens == tokens_to_yield
    assert lines[-1] == "data: [DONE]"

    detail_res = client.get(f"/api/v1/assistant/conversations/{conv_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["id"] == conv_id
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["role"] == "user"
    assert detail["messages"][0]["content"] == "Hello assistant!"
    assert detail["messages"][1]["role"] == "assistant"
    assert detail["messages"][1]["content"] == "".join(tokens_to_yield)


def test_missing_required_details_persist_and_resume_same_conversation(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("clarification must not reach the LLM"),
    )

    first = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Schedule a meeting with Maya tomorrow"},
    )
    first_events = _sse_events(first)
    conversation_id = first_events[0]["conversation_id"]

    assert first_events[0]["understanding"]["status"] == "needs_clarification"
    assert "What time" in first_events[1]["token"]
    conversation = db_session.get(AssistantConversation, conversation_id)
    state = json.loads(conversation.pending_clarification)
    assert state["clarification_count"] == 1
    assert state["understanding"]["intent"] == "meeting_scheduling"
    assert state["understanding"]["missing_entities"] == ["time"]
    assert db_session.query(Meeting).count() == 0
    assert db_session.query(Task).count() == 0

    second = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "3 PM", "conversation_id": conversation_id},
    )
    second_events = _sse_events(second)

    assert second_events[0]["conversation_id"] == conversation_id
    assert second_events[0]["understanding"]["status"] == "understood"
    assert "All required details are resolved" in second_events[1]["token"]
    assert db_session.get(AssistantConversation, conversation_id).pending_clarification is None
    assert len(db_session.query(AssistantMessage).filter_by(
        conversation_id=conversation_id,
    ).all()) == 4
    # Once resolved, the request runs its plan (Stories 6.5-6.7); whether the
    # meeting is booked now or waits for approval depends on its risk (6.6).
    assert any("plan" in event for event in second_events)


def test_ambiguous_employee_name_requires_explicit_selection(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    db_session.add_all([
        Employee(
            id="emp-rahul-one",
            name="Rahul Sharma",
            email="rahul.sharma@northstar.example",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="emp-rahul-two",
            name="Rahul Verma",
            email="rahul.verma@northstar.example",
            team="Design",
            is_active=True,
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("recognized requests must not reach the LLM"),
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Assign the database task to Rahul"},
    )
    events = _sse_events(response)
    conversation_id = events[0]["conversation_id"]

    assert events[0]["understanding"]["status"] == "needs_clarification"
    assert events[0]["understanding"]["entities"]["people"] == []
    assert events[0]["understanding"]["entities"]["ambiguous_people"] == {
        "Rahul": ["Rahul Sharma", "Rahul Verma"],
    }
    assert "Which one did you mean?" in events[1]["token"]

    resolved = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Rahul Sharma", "conversation_id": conversation_id},
    )
    resolved_events = _sse_events(resolved)
    assert resolved_events[0]["understanding"]["status"] == "understood"
    assert resolved_events[0]["understanding"]["entities"]["people"] == ["Rahul Sharma"]
    assert db_session.get(AssistantConversation, conversation_id).pending_clarification is None


def test_ambiguous_meeting_attendee_requires_selection_without_scheduling(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    db_session.add_all([
        Employee(
            id="emp-meeting-rahul-one",
            name="Rahul",
            email="rahul.one@northstar.example",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="emp-meeting-rahul-two",
            name="Rahul Verma",
            email="rahul.verma@northstar.example",
            team="Design",
            is_active=True,
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("recognized requests must not reach the LLM"),
    )
    monkeypatch.setattr(
        "app.meeting.scheduling_service.schedule_meeting_from_natural_language",
        lambda *_args, **_kwargs: pytest.fail(
            "ambiguous meeting requests must not reach scheduling"
        ),
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Schedule a meeting with Rahul tomorrow at 10 AM."},
    )
    events = _sse_events(response)

    assert response.status_code == 200
    assert events[0]["understanding"]["intent"] == "meeting_scheduling"
    assert events[0]["understanding"]["status"] == "needs_clarification"
    assert events[0]["understanding"]["entities"]["ambiguous_people"] == {
        "Rahul": ["Rahul", "Rahul Verma"],
    }
    assert events[0]["understanding"]["entities"]["people"] == []
    clarification = events[1]["token"]
    assert "Which one did you mean?" in clarification
    assert "Rahul" in clarification
    assert "Rahul Verma" in clarification
    assert db_session.query(Meeting).count() == 0


def test_full_name_resolves_one_candidate_when_other_employee_has_same_first_name(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    db_session.add_all([
        Employee(
            id="emp-full-name-rahul-one",
            name="Rahul",
            email="rahul.one@northstar.example",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="emp-full-name-rahul-two",
            name="Rahul Verma",
            email="rahul.verma@northstar.example",
            team="Design",
            is_active=True,
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("recognized requests must not reach the LLM"),
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Schedule a meeting with Rahul Verma tomorrow at 10 AM."},
    )
    events = _sse_events(response)

    assert events[0]["understanding"]["status"] == "understood"
    assert events[0]["understanding"]["entities"]["people"] == ["Rahul Verma"]
    assert events[0]["understanding"]["entities"]["ambiguous_people"] == {}
    # Whatever the plan books (by risk, Story 6.6), never the other Rahul.
    invited = {participant.email for participant in db_session.query(MeetingParticipant).all()}
    assert "rahul.one@northstar.example" not in invited


def test_clarification_question_limit_is_two_but_pending_answer_can_resolve(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *_args, **_kwargs: pytest.fail("clarification must not reach the LLM"),
    )

    first = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Schedule a meeting"},
    )
    first_events = _sse_events(first)
    conversation_id = first_events[0]["conversation_id"]

    second = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "the Engineering team", "conversation_id": conversation_id},
    )
    second_events = _sse_events(second)
    assert "What date" in second_events[1]["token"]
    assert second_events[0]["clarification_count"] == 2

    third = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "tomorrow", "conversation_id": conversation_id},
    )
    third_events = _sse_events(third)
    assert "two-question limit" in third_events[1]["token"]
    assert "?" not in third_events[1]["token"]
    assert third_events[0]["clarification_count"] == 2
    assert sum(
        events[1]["token"].count("?")
        for events in (first_events, second_events, third_events)
    ) == 2
    state = json.loads(
        db_session.get(AssistantConversation, conversation_id).pending_clarification
    )
    assert state["clarification_count"] == 2
    assert state["pending_field"] == "time"

    fourth = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "4 PM", "conversation_id": conversation_id},
    )
    fourth_events = _sse_events(fourth)
    assert fourth_events[0]["understanding"]["status"] == "understood"
    assert "All required details are resolved" in fourth_events[1]["token"]
    assert db_session.get(AssistantConversation, conversation_id).pending_clarification is None


def test_clarification_can_be_canceled_without_executing(
    client: TestClient,
    db_session: Session,
):
    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Schedule a meeting with Maya tomorrow"},
    )
    conversation_id = _sse_events(response)[0]["conversation_id"]

    canceled = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "cancel request", "conversation_id": conversation_id},
    )
    events = _sse_events(canceled)

    assert "canceled" in events[1]["token"]
    assert db_session.get(AssistantConversation, conversation_id).pending_clarification is None
    assert db_session.query(Meeting).count() == 0
    assert db_session.query(Task).count() == 0


def test_measure_time_to_first_response_chunk(client: TestClient, monkeypatch):
    """Measure the time-to-first-token under local test conditions."""
    def mock_stream(messages, temperature=0.3):
        time.sleep(0.05)
        yield "First chunk"
        yield " Second chunk"

    monkeypatch.setattr(assistant_service, "stream_chat_completion", mock_stream)

    start_time = time.perf_counter()

    with client.stream(
        "POST",
        "/api/v1/assistant/chat/stream",
        json={"message": "Quick benchmark check"},
    ) as response:
        assert response.status_code == 200
        first_token_time = None
        for line in response.iter_lines():
            if line and "token" in line:
                first_token_time = time.perf_counter() - start_time
                break

    assert first_token_time is not None
    assert first_token_time < 2.0
    print(f"\n[LATENCY BENCHMARK] Time to first response chunk: {first_token_time * 1000:.2f} ms")


def test_multi_turn_conversation_history_retention(client: TestClient, monkeypatch):
    """Verify that multiple turns under the same conversation_id retain history."""
    replies = ["First answer.", "Second answer."]
    turn_counter = 0

    def mock_stream(messages, temperature=0.3):
        nonlocal turn_counter
        reply = replies[turn_counter % len(replies)]
        turn_counter += 1
        yield reply

    monkeypatch.setattr(assistant_service, "stream_chat_completion", mock_stream)

    res1 = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Question 1"},
    )
    assert res1.status_code == 200
    first_line = [l for l in res1.text.split("\n") if l.startswith("data:")][0]
    conv_id = json.loads(first_line.replace("data:", "").strip())["conversation_id"]

    res2 = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Question 2", "conversation_id": conv_id},
    )
    assert res2.status_code == 200

    history_res = client.get(f"/api/v1/assistant/conversations/{conv_id}")
    assert history_res.status_code == 200
    history = history_res.json()["messages"]
    assert len(history) == 4
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Question 1"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "First answer."
    assert history[2]["role"] == "user"
    assert history[2]["content"] == "Question 2"
    assert history[3]["role"] == "assistant"
    assert history[3]["content"] == "Second answer."


def test_list_and_delete_user_conversations(
    client: TestClient,
    principal_a: AuthenticatedPrincipal,
    monkeypatch,
):
    """Verify authenticated users can list and delete their conversation history."""
    def mock_stream(messages, temperature=0.3):
        yield "Sample reply"

    monkeypatch.setattr(assistant_service, "stream_chat_completion", mock_stream)

    res1 = client.post("/api/v1/assistant/chat/stream", json={"message": "Chat A"})
    line1 = [l for l in res1.text.split("\n") if l.startswith("data:")][0]
    conv_a = json.loads(line1.replace("data:", "").strip())["conversation_id"]

    res2 = client.post("/api/v1/assistant/chat/stream", json={"message": "Chat B"})
    line2 = [l for l in res2.text.split("\n") if l.startswith("data:")][0]
    conv_b = json.loads(line2.replace("data:", "").strip())["conversation_id"]

    list_res = client.get("/api/v1/assistant/conversations")
    assert list_res.status_code == 200
    items = list_res.json()
    conv_ids = [c["id"] for c in items]
    assert conv_a in conv_ids
    assert conv_b in conv_ids

    del_res = client.delete(f"/api/v1/assistant/conversations/{conv_a}")
    assert del_res.status_code == 204

    get_res = client.get(f"/api/v1/assistant/conversations/{conv_a}")
    assert get_res.status_code == 404


def test_invalid_request_handling(client: TestClient):
    """Verify validation on empty messages and invalid conversation IDs."""
    empty_res = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": ""},
    )
    assert empty_res.status_code == 422

    not_found_res = client.get("/api/v1/assistant/conversations/non-existent-uuid")
    assert not_found_res.status_code == 404


def test_safe_error_handling_without_leaking_secrets(client: TestClient, monkeypatch):
    """Verify that model generation errors return clean SSE error events without leaking API keys."""
    def mock_failing_stream(messages, temperature=0.3):
        raise RuntimeError("API key gsk_secret_12345 failed authentication at provider")
        yield ""

    monkeypatch.setattr(assistant_service, "stream_chat_completion", mock_failing_stream)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Cause error"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    lines = [l.strip() for l in response.text.split("\n") if l.strip()]
    assert "gsk_secret_12345" not in response.text

    error_lines = [l for l in lines if "error" in l]
    assert len(error_lines) == 1
    err_data = json.loads(error_lines[0].replace("data:", "").strip())
    assert "error" in err_data
    assert "An error occurred while generating the response." in err_data["error"]
    assert lines[-1] == "data: [DONE]"


def test_live_groq_streaming_and_latency(client: TestClient):
    """Live integration test: connects to Groq API and measures actual first-token latency.

    Skipped if no GROQ_API_KEY is configured.
    """
    import os
    if not os.environ.get("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY is not configured in backend/.env")

    start_time = time.perf_counter()
    first_token_time = None
    accumulated_tokens = []

    with client.stream(
        "POST",
        "/api/v1/assistant/chat/stream",
        json={"message": "Reply with only 'Hello NorthStar' and nothing else."},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line and "token" in line:
                if first_token_time is None:
                    first_token_time = time.perf_counter() - start_time
                try:
                    payload = json.loads(line.replace("data:", "").strip())
                    if "token" in payload:
                        accumulated_tokens.append(payload["token"])
                except Exception:
                    pass

    total_time = time.perf_counter() - start_time
    assert first_token_time is not None
    full_text = "".join(accumulated_tokens)
    print(f"\n[LIVE GROQ BENCHMARK]")
    print(f"Time to first token: {first_token_time * 1000:.2f} ms")
    print(f"Total stream duration: {total_time * 1000:.2f} ms")
    print(f"Generated text: {full_text!r}")

    assert first_token_time < 2.0
