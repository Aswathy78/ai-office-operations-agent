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
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
import app.assistant_service as assistant_service


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
