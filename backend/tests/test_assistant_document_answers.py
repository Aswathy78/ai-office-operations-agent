"""Story 7.3 / FR-39 — answers from company documents in chat.

Complements test_assistant_semantic_search.py. Coverage:

Retrieval   The model receives exactly the best 4 of the 8 ACL-filtered matches.
Categories  HR policies, SOPs and project documents are all found by real search.
Grounding   The model is told to answer only from the passages and cite them.
Not found   Nothing relevant -> a clear "couldn't find" answer, no model call and
            no ungated fallback to the static policy file; a search failure too.
Access      Documents shared with all employees are readable by active employees;
            private ones are not. Upload accepts the "shared" option.
Workflows   E-mail drafts say when no document facts were found; plan requests are
            grounded; other actions never trigger document answers.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import assistant_planner, assistant_service
from app.assistant_understanding import understand_assistant_command
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.documents import indexing
from app.documents.indexing import SemanticSearchResult
from app.documents.models import (
    DOC_STATUS_READY,
    INDEX_STATUS_PENDING,
    INDEX_STATUS_READY,
    Document,
    DocumentACLEntry,
    DocumentChunk,
)
from app.meeting.models import Base, Employee


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add_all([
        Employee(id="doc-hr", name="Hana Reyes", email="hana@northstar.example",
                 team="HR", is_active=True),
        Employee(id="doc-emp", name="Evan Cole", email="evan@northstar.example",
                 team="Engineering", is_active=True),
        Employee(id="doc-maya", name="Maya Thompson", email="maya@northstar.example",
                 team="Engineering", is_active=True),
    ])
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _principal(user_id: str, role: str = "employee") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user_id, account_id=f"acc-{user_id}", email=f"{user_id}@northstar.example",
        name="Evan Cole" if user_id == "doc-emp" else "Hana Reyes", role=role,
        team="Engineering", jti=f"jti-{user_id}",
    )


@pytest.fixture()
def client(db_session: Session):
    current = {"principal": _principal("doc-emp")}
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: current["principal"]
    with TestClient(app) as test_client:
        test_client.login = lambda user_id, role="employee": current.update(
            principal=_principal(user_id, role)
        )
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def deterministic_embeddings(monkeypatch: pytest.MonkeyPatch):
    """Every passage is equally similar, so the lexical rules decide relevance."""
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])
    monkeypatch.setattr(indexing, "embed_passages", lambda passages: [[1.0, 0.0] for _ in passages])


@pytest.fixture()
def no_llm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (None, None, None))


def _add_document(
    db: Session,
    *,
    filename: str,
    text: str,
    owner_id: str = "doc-hr",
    shared: bool = True,
    department: str = "HR",
) -> Document:
    document = Document(
        id=str(uuid.uuid4()), original_filename=filename, stored_filename=f"{uuid.uuid4()}.pdf",
        file_size=100, mime_type="application/pdf", owner_id=owner_id,
        status=DOC_STATUS_READY, indexing_status=INDEX_STATUS_READY, version=1,
        extracted_text=text, shared_with_all_employees=shared,
    )
    db.add(document)
    db.flush()
    db.add(DocumentChunk(
        id=str(uuid.uuid4()), document_id=document.id, document_version=1, chunk_index=0,
        heading=None, content=text, token_count=40, department=department,
        acl_employee_ids=[owner_id], embedding=[1.0, 0.0],
        embedding_model=indexing.EMBEDDING_MODEL,
    ))
    db.commit()
    return document


HR_POLICY = (
    "# Leave Policy\n\n"
    "Casual Leave: Employees may take up to 8 days per calendar year.\n\n"
    "## Working Hours\n\n"
    "Employee working hours are Monday through Friday, 09:30–18:00 IST."
)
SOP = (
    "# Incident Response SOP\n\n"
    "## Escalation Steps\n\n"
    "Report the incident to the on-call engineer within 15 minutes. "
    "The on-call engineer opens a severity ticket and informs the IT manager."
)
PROJECT = (
    "# Apollo Project Plan\n\n"
    "## Milestones\n\n"
    "The Apollo beta launch milestone is 14 November 2026, after the security review."
)


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]


def _answer(events: list[dict]) -> str:
    return "".join(event.get("token", "") for event in events)


def _ask(client: TestClient, message: str) -> list[dict]:
    response = client.post("/api/v1/assistant/chat/stream", json={"message": message})
    assert response.status_code == 200
    return _events(response)


# ---------------------------------------------------------------------------
# Retrieval: top 8 -> best 4, all document categories
# ---------------------------------------------------------------------------


def test_chat_passes_exactly_the_best_four_of_eight_matches_to_the_model(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch,
):
    limits = []
    for i in range(8):
        db_session.add(Document(
            id=f"doc-{i}", original_filename=f"policy-{i}.pdf", stored_filename=f"s-{i}.pdf",
            file_size=1, mime_type="application/pdf", owner_id="doc-hr",
            status=DOC_STATUS_READY, indexing_status=INDEX_STATUS_READY, version=1,
            shared_with_all_employees=True,
        ))
    db_session.commit()

    def search(db, *, actor_id, query, limit):
        limits.append(limit)
        return [
            SemanticSearchResult(
                document_id=f"doc-{i}", filename=f"policy-{i}.pdf", heading=None,
                content=f"The carry forward limit is {i} days.", score=1.0 - i / 10,
                document_version=1,
            )
            for i in range(limit)
        ]

    received = []
    monkeypatch.setattr(indexing, "search_documents", search)
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda messages, **kwargs: received.append(kwargs.get("rag_context"))
        or iter(["The carry forward limit is 0 days."]),
    )

    events = _ask(client, "What does the leave policy say about carry forward?")

    assert limits == [8]
    assert [item["snippet"] for item in received[0]] == [
        f"The carry forward limit is {i} days." for i in range(4)
    ]
    # Four passages reach the model, but only the one that states the answer is cited.
    [retrieval] = [event["retrieval"] for event in events if "retrieval" in event]
    assert retrieval["sources"] == ["policy-0.pdf"]


@pytest.mark.parametrize(
    ("question", "filename", "expected"),
    [
        ("How many days of casual leave can an employee take?", "leave-policy.pdf", "8 days"),
        ("What are the escalation steps in the incident response SOP?",
         "incident-sop.docx", "within 15 minutes"),
        ("When is the Apollo project beta launch milestone?", "apollo-plan.pptx", "14 November 2026"),
    ],
)
def test_hr_sop_and_project_documents_are_all_answerable(
    client: TestClient, db_session: Session, deterministic_embeddings, no_llm,
    question: str, filename: str, expected: str,
):
    _add_document(db_session, filename="leave-policy.pdf", text=HR_POLICY)
    _add_document(db_session, filename="incident-sop.docx", text=SOP, department="IT")
    _add_document(db_session, filename="apollo-plan.pptx", text=PROJECT, department="Engineering")

    answer = _answer(_ask(client, question))

    assert expected in answer
    assert filename in answer


@pytest.mark.parametrize(
    "question",
    [
        "How many days of casual leave can an employee take?",
        "How many casual leaves do I get?",
        "How much casual time off is an employee entitled to?",
    ],
)
def test_question_verbs_do_not_hide_the_handbook_answer(
    client: TestClient, db_session: Session, deterministic_embeddings, no_llm, question: str,
):
    # The real seeded handbook's wording: the verb in the question never appears.
    _add_document(db_session, filename="handbook.pdf", text=(
        "3.1 Leave entitlements (per calendar year):\n"
        "- Earned leave: 18 days (Accrues 1.5 days per month).\n"
        "- Casual leave: 8 days (Maximum 3 consecutive days; no carry forward).\n"
    ))

    answer = _answer(_ask(client, question))

    assert "Casual leave: 8 days" in answer


def test_indexed_handbook_table_row_is_searchable_for_acl_authorized_employee(
    client: TestClient,
    db_session: Session,
    deterministic_embeddings,
    no_llm,
    monkeypatch: pytest.MonkeyPatch,
):
    source_text = (
        "3. Leave, Attendance and Holidays\n\n"
        "Casual leave | 8 days | Maximum 3 consecutive days"
    )
    document = Document(
        id=str(uuid.uuid4()),
        original_filename="HR_Operations_Knowledge_Handbook.pdf",
        stored_filename=f"{uuid.uuid4()}.pdf",
        file_size=100,
        mime_type="application/pdf",
        owner_id="doc-hr",
        status=DOC_STATUS_READY,
        indexing_status=INDEX_STATUS_PENDING,
        version=2,
        extracted_text=source_text,
        shared_with_all_employees=False,
    )
    db_session.add(document)
    db_session.flush()
    db_session.add(DocumentACLEntry(
        id=str(uuid.uuid4()),
        document_id=document.id,
        employee_id="doc-emp",
    ))
    db_session.add(DocumentChunk(
        id=str(uuid.uuid4()),
        document_id=document.id,
        document_version=1,
        chunk_index=0,
        heading="Leave entitlements",
        content="Casual leave | 30 days | Historical, superseded value",
        token_count=9,
        department="HR",
        soft_deleted_at=datetime.now(timezone.utc),
        acl_employee_ids=["doc-hr", "doc-emp"],
        embedding=[1.0, 0.0],
        embedding_model=indexing.EMBEDDING_MODEL,
    ))
    db_session.commit()

    monkeypatch.setattr(indexing, "count_tokens", lambda text: len(text.split()))
    assert indexing.index_document(db_session, document.id)
    db_session.refresh(document)
    assert document.indexing_status == INDEX_STATUS_READY

    results = indexing.search_documents(
        db_session,
        actor_id="doc-emp",
        query="How many casual leave days do employees get?",
    )
    assert results
    assert "8 days" in results[0].content
    assert "Maximum 3 consecutive days" in results[0].content
    assert all("30 days" not in result.content for result in results)
    assert indexing.search_documents(
        db_session,
        actor_id="doc-maya",
        query="How many casual leave days do employees get?",
    ) == []

    answer = _answer(
        _ask(client, "How many casual leave days do employees get?")
    )
    assert "8 days" in answer
    assert "Maximum 3 consecutive days" in answer


# ---------------------------------------------------------------------------
# Grounding instructions
# ---------------------------------------------------------------------------


def test_model_is_told_to_answer_only_from_the_cited_passages(monkeypatch: pytest.MonkeyPatch):
    sent = []

    class FakeCompletions:
        def create(self, **kwargs):
            sent.append(kwargs["messages"])
            return iter([])

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (FakeClient(), "m", "groq"))
    context = [
        {"source": f"doc-{i}.pdf", "heading": "Leave", "snippet": f"Fact {i}."}
        for i in range(4)
    ]

    list(assistant_service.stream_chat_completion(
        [{"role": "user", "content": "How much leave?"}], rag_context=context
    ))

    grounding = next(m["content"] for m in sent[0] if "passages" in m["content"])
    assert "ONLY these passages" in grounding
    assert "reply with exactly: Not found" in grounding
    assert "UNTRUSTED DATA" in grounding
    assert all(f"doc-{i}.pdf" in grounding and f"Fact {i}." in grounding for i in range(4))


def test_offline_answer_cites_the_other_sources_too(no_llm):
    context = [
        {"source": "a.pdf", "snippet": "First."},
        {"source": "b.pdf", "snippet": "Second."},
        {"source": "a.pdf", "snippet": "Third."},
    ]
    answer = "".join(assistant_service.stream_chat_completion([], rag_context=context))
    assert "First." in answer
    assert "Also relevant: b.pdf." in answer


# ---------------------------------------------------------------------------
# Insufficient context
# ---------------------------------------------------------------------------


def test_no_relevant_document_answers_not_found_without_static_fallback(
    client: TestClient, db_session: Session, deterministic_embeddings,
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    _add_document(db_session, filename="leave-policy.pdf", text=HR_POLICY)
    static_policy = tmp_path / "company_policy.md"
    static_policy.write_text("Remote work policy: two days per week from home.\n", encoding="utf-8")
    monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", static_policy)
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda *args, **kwargs: pytest.fail("No model call without relevant documents."),
    )

    events = _ask(client, "What is the company policy on remote work allowances?")

    assert _answer(events) == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    assert "two days per week" not in _answer(events)
    assert not any("retrieval" in event for event in events)


def test_search_failure_is_reported_without_guessing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    def broken(*args, **kwargs):
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(assistant_planner, "retrieve_semantic_document_context", broken)
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda *args, **kwargs: pytest.fail("No model call when search failed."),
    )

    events = _ask(client, "What does the HR policy say about probation?")

    assert _answer(events) == assistant_planner.DOCUMENT_SEARCH_FAILED_MESSAGE


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_private_documents_are_not_used_for_other_employees(
    client: TestClient, db_session: Session, deterministic_embeddings, no_llm,
):
    _add_document(db_session, filename="hr-private-pay-bands.pdf", owner_id="doc-hr",
                  shared=False, text="# Pay Bands\n\nCasual Leave: Employees may take up to 30 days.")

    answer = _answer(_ask(client, "How many days of casual leave can an employee take?"))

    assert answer == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    assert "30 days" not in answer


def test_superseded_document_content_is_not_used_in_generated_answers(
    client: TestClient, db_session: Session, deterministic_embeddings, no_llm,
):
    document = _add_document(
        db_session,
        filename="old-leave-policy.pdf",
        text=(
            "# Leave Policy\n\n"
            "Casual Leave: Employees may take up to 30 days per calendar year."
        ),
    )
    old_chunk = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .one()
    )
    old_chunk.soft_deleted_at = datetime.now(timezone.utc)
    document.version = 2
    document.indexing_status = INDEX_STATUS_PENDING
    db_session.commit()

    answer = _answer(
        _ask(client, "How many days of casual leave can an employee take?")
    )

    assert answer == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    assert "30 days" not in answer


def test_documents_shared_with_all_employees_are_answerable_by_every_active_employee(
    client: TestClient, db_session: Session, deterministic_embeddings, no_llm,
):
    _add_document(db_session, filename="leave-policy.pdf", text=HR_POLICY, shared=True)

    answer = _answer(_ask(client, "How many days of casual leave can an employee take?"))

    assert "8 days" in answer and "leave-policy.pdf" in answer


def test_upload_accepts_the_shared_with_all_employees_option(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch,
):
    from app.documents import router as documents_router

    monkeypatch.setattr(documents_router, "_require_manager_or_hr", lambda principal: None)
    client.login("doc-hr", role="hr_operations")
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
        b"\x00\x00IEND\xaeB`\x82"
    )

    response = client.post(
        "/api/v1/documents/upload",
        files={"file": ("handbook.png", png, "image/png")},
        data={"shared_with_all_employees": "true"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["document"]["shared_with_all_employees"] is True
    stored = db_session.get(Document, response.json()["document"]["id"])
    assert stored.shared_with_all_employees is True


# ---------------------------------------------------------------------------
# E-mail and plan workflows
# ---------------------------------------------------------------------------


def test_assistant_email_without_document_facts_says_so_and_stays_editable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(assistant_planner, "retrieve_semantic_document_context",
                        lambda db, *, actor_id, query: [])
    monkeypatch.setattr(
        assistant_planner, "_search_static_company_knowledge", lambda query, limit: []
    )

    events = _ask(client, "Send an email to Maya about the project kickoff")

    assert assistant_planner.EMAIL_DOCUMENT_NOT_FOUND_NOTICE in _answer(events)
    # Drafting continues without invented facts; the user reviews it before sending.
    assert any("awaiting_confirmation" in event for event in events)


def test_email_policy_fallback_does_not_repeat_semantic_search(
    db_session: Session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    static_policy = tmp_path / "company_policy.md"
    static_policy.write_text(
        "Flexible core hours are 11:00-16:00 IST.\n",
        encoding="utf-8",
    )
    calls = []

    def no_indexed_match(db, *, actor_id, query):
        calls.append((actor_id, query))
        return []

    monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", static_policy)
    monkeypatch.setattr(
        assistant_planner, "retrieve_semantic_document_context", no_indexed_match
    )

    results = assistant_planner.retrieve_email_document_context(
        db_session,
        actor_id="doc-emp",
        topic="the company's flexible core hours",
    )

    assert len(calls) == 1
    assert calls[0] == ("doc-emp", "flexible core hours")
    assert results[0]["snippet"] == "Flexible core hours are 11:00-16:00 IST."
    assert results[0]["source"] == "company_policy.md"


def test_email_document_search_timeout_falls_back_to_reviewable_draft(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    def timed_out(*args, **kwargs):
        raise TimeoutError("document.search timed out")

    monkeypatch.setattr(assistant_planner, "retrieve_semantic_document_context", timed_out)
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion", lambda *args, **kwargs: iter(["Draft."])
    )

    events = _ask(client, "Send an email to Maya about the project kickoff")

    assert assistant_planner.EMAIL_DOCUMENT_NOT_FOUND_NOTICE in _answer(events)
    assert any("awaiting_confirmation" in event for event in events)


def test_document_plan_request_is_grounded_in_real_search_results(
    client: TestClient, db_session: Session, deterministic_embeddings,
    monkeypatch: pytest.MonkeyPatch,
):
    _add_document(db_session, filename="incident-sop.docx", text=SOP, department="IT")
    received = []
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda messages, **kwargs: received.append(kwargs.get("rag_context")) or iter(["Plan."]),
    )

    _ask(client, "Create an incident escalation plan based on the incident response SOP.")

    assert received and received[0][0]["source"] == "incident-sop.docx"
    assert "15 minutes" in received[0][0]["snippet"]


def test_other_actions_mentioning_policies_are_not_answered_from_documents(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        assistant_planner, "retrieve_semantic_document_context",
        lambda *args, **kwargs: pytest.fail("An unsupported action is not a document question."),
    )

    events = _ask(client, "Delete the HR policy document.")

    assert "can't handle that operation yet" in _answer(events)


def test_email_topic_understands_explaining_and_where_to_look(db_session: Session):
    result = understand_assistant_command(
        db_session,
        "Send an email to Maya explaining the company's flexible core hours "
        "according to the HR handbook.",
    )
    assert result is not None and result.status == "understood"
    assert result.entities.topic == "the company's flexible core hours"
    assert assistant_planner.email_document_query(result.entities.topic) == "flexible core hours"


# ---------------------------------------------------------------------------
# Questions without a policy keyword are still grounded in the documents
# ---------------------------------------------------------------------------

REMOTE_QUESTION = "How many remote work days are allowed per year?"
REMOTE_TEXT = "# Remote Work Policy\n\nRemote work: employees may work remotely from another city for up to 15 days per year."


def _remote_policy_for_maya_only(db: Session) -> Document:
    document = _add_document(
        db, filename="Remote-Work-Policy.pdf", text=REMOTE_TEXT, owner_id="doc-hr", shared=False,
    )
    db.add(DocumentACLEntry(id=str(uuid.uuid4()), document_id=document.id, employee_id="doc-maya"))
    db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).update(
        {DocumentChunk.acl_employee_ids: ["doc-hr", "doc-maya"]}
    )
    db.commit()
    return document


@pytest.mark.parametrize("message", [
    REMOTE_QUESTION,
    "Can I work from home on Fridays?",
    "How do I get a parking permit?",
    "what is the travel allowance",
])
def test_questions_without_a_policy_keyword_are_searched_in_the_documents(message: str):
    assert assistant_planner.should_search_documents(message)


@pytest.mark.parametrize("message", ["ok", "full time", "hello there", "Schedule a meeting tomorrow"])
def test_statements_and_commands_are_not_treated_as_document_questions(message: str):
    assert not assistant_planner.looks_like_question(message)


def test_remote_work_question_is_answered_for_a_person_on_the_access_list(
    client: TestClient, db_session: Session, deterministic_embeddings, no_llm,
):
    _remote_policy_for_maya_only(db_session)
    client.login("doc-maya")

    events = _ask(client, REMOTE_QUESTION)

    assert any("retrieval" in event for event in events)
    assert "15 days" in _answer(events)
    assert "Remote-Work-Policy.pdf" in _answer(events)


def test_remote_work_question_is_not_answered_for_someone_off_the_access_list(
    client: TestClient, db_session: Session, deterministic_embeddings, monkeypatch: pytest.MonkeyPatch,
):
    _remote_policy_for_maya_only(db_session)
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda *args, **kwargs: pytest.fail("The model must not answer from memory."),
    )

    events = _ask(client, REMOTE_QUESTION)

    assert _answer(events) == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    assert "15" not in _answer(events)


def test_chat_without_documents_forbids_inventing_company_facts(
    client: TestClient, db_session: Session, deterministic_embeddings, monkeypatch: pytest.MonkeyPatch,
):
    sent = []

    class FakeCompletions:
        def create(self, **kwargs):
            sent.append(kwargs["messages"])
            return iter([])

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (FakeClient(), "m", "groq"))

    _ask(client, "How do I find the parking garage?")

    guard = [m["content"] for m in sent[0] if m["role"] == "system" and "No company documents were retrieved" in m["content"]]
    assert len(guard) == 1
    assert "Never state specific company policy facts" in guard[0]
