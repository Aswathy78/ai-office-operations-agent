from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import assistant_planner, assistant_service
from app import assistant_router
from app import assistant_tool_catalog, email_draft_service
from app.assistant_planner import (
    DOCUMENT_SEARCH_CONTEXT_LIMIT,
    DOCUMENT_SEARCH_RETRIEVAL_LIMIT,
    prepare_tool_arguments,
    retrieve_semantic_document_context,
)
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.assistant_tools import ToolContext
from app.db.database import get_db
from app.documents import indexing
from app.documents.indexing import SemanticSearchResult
from app.documents.models import (
    DOC_STATUS_READY,
    INDEX_STATUS_READY,
    Document,
    DocumentACLEntry,
    DocumentChunk,
)
from app.meeting.models import Base, EmailDraft, Employee


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def employee(db_session: Session):
    person = Employee(
        id="semantic-employee",
        name="Diana Prince",
        email="diana@northstar.example",
        team="HR",
        is_active=True,
    )
    db_session.add(person)
    db_session.commit()
    return person


@pytest.fixture()
def principal(employee: Employee):
    return AuthenticatedPrincipal(
        user_id=employee.id,
        account_id="semantic-account",
        email=employee.email,
        name=employee.name,
        role="employee",
        team=employee.team,
        jti="semantic-session",
    )


@pytest.fixture()
def client(db_session: Session, principal: AuthenticatedPrincipal):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]


def _search_result(index: int) -> SemanticSearchResult:
    return SemanticSearchResult(
        document_id=f"doc-{index}",
        filename=f"project-{index}.docx",
        heading=f"Section {index}",
        content=f"Relevant project passage {index}",
        score=1.0 - index / 100,
        document_version=1,
    )


def _add_indexed_handbook(db_session: Session, employee: Employee) -> Document:
    document = Document(
        id=str(uuid.uuid4()),
        original_filename="HR_and_Operations_Knowledge_Handbook_v2.1.pdf",
        stored_filename=f"{uuid.uuid4()}.pdf",
        file_size=100,
        mime_type="application/pdf",
        owner_id=employee.id,
        status=DOC_STATUS_READY,
        indexing_status=INDEX_STATUS_READY,
        version=2,
        extracted_text=(
            "# HR and Operations Knowledge Handbook\n\n"
            "## Leave Entitlements\n\n"
            "Casual Leave: Employees may take up to 8 days per calendar year.\n\n"
            "Maternity Leave: Employees are provided 26 weeks of maternity leave.\n\n"
            "## Working Hours\n\n"
            "Employee working hours are Monday through Friday, "
            "09:30–18:00 IST, with flexible core hours from 11:00–16:00 IST."
        ),
    )
    db_session.add(document)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            id=str(uuid.uuid4()),
            document_id=document.id,
            document_version=document.version,
            chunk_index=0,
            heading="HR and Operations Knowledge Handbook",
            content=document.extracted_text,
            token_count=60,
            department="HR",
            acl_employee_ids=[employee.id],
            embedding=[1.0, 0.0],
            embedding_model=indexing.EMBEDDING_MODEL,
        )
    )
    db_session.commit()
    return document


def _use_deterministic_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: [[1.0, 0.0] for _ in passages],
    )


def test_semantic_context_retrieves_eight_and_selects_best_four(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    def search_documents(db, *, actor_id, query, limit):
        calls.append((actor_id, query, limit))
        return [_search_result(index) for index in range(limit)]

    monkeypatch.setattr(indexing, "search_documents", search_documents)

    context = retrieve_semantic_document_context(
        db_session,
        actor_id="employee-1",
        query="project launch plan",
    )

    assert calls == [("employee-1", "project launch plan", 8)]
    assert DOCUMENT_SEARCH_RETRIEVAL_LIMIT == 8
    assert DOCUMENT_SEARCH_CONTEXT_LIMIT == 4
    assert [item["document_id"] for item in context] == [
        "doc-0",
        "doc-1",
        "doc-2",
        "doc-3",
    ]


@pytest.mark.parametrize(
    ("query", "expected_value"),
    [
        ("How many days of casual leave can an employee take?", "8 days"),
        ("How much casual time off can an employee take?", "8 days"),
        ("What is the casual leave entitlement?", "8 days"),
        ("How many days of casual leave are provided?", "8 days"),
        ("How many weeks of maternity leave are provided?", "26 weeks"),
        (
            "What does the HR handbook say about maternity leave?",
            "26 weeks",
        ),
        ("What are the employee working hours?", "09:30–18:00 IST"),
        ("What are the working hours?", "09:30–18:00 IST"),
        (
            "What does the HR handbook say about employee working hours?",
            "09:30–18:00 IST",
        ),
    ],
)
def test_natural_language_hr_questions_retrieve_indexed_handbook(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_value: str,
):
    _add_indexed_handbook(db_session, employee)
    _use_deterministic_embeddings(monkeypatch)
    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (None, None, None))

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": query},
    )
    events = _events(response)
    answer = "".join(event.get("token", "") for event in events)

    assert response.status_code == 200
    assert expected_value in answer
    assert "HR_and_Operations_Knowledge_Handbook_v2.1.pdf" in answer
    assert "Northwind Employee Handbook" not in answer


@pytest.mark.parametrize(
    "query",
    [
        "What are the employee working hours according to the HR handbook?",
        "What does the HR handbook say about employee working hours?",
    ],
)
def test_employee_acl_retrieves_working_hours_for_handbook_phrasings(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
):
    owner = Employee(
        id="handbook-owner",
        name="Handbook Owner",
        email="handbook-owner@northstar.example",
        team="HR",
        is_active=True,
    )
    db_session.add(owner)
    db_session.commit()
    document = _add_indexed_handbook(db_session, owner)
    db_session.add(
        DocumentACLEntry(
            id=str(uuid.uuid4()),
            document_id=document.id,
            employee_id=employee.id,
        )
    )
    db_session.add_all(
        [
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=document.id,
                document_version=document.version,
                chunk_index=chunk_index,
                content=(
                    "Working hours are Monday through Friday, "
                    "09:00-17:00 IST."
                ),
                token_count=10,
                department="Operations",
                acl_employee_ids=[owner.id, employee.id],
                embedding=[1.0, 0.0],
                embedding_model=indexing.EMBEDDING_MODEL,
            )
            for chunk_index in range(1, 8)
        ]
    )
    db_session.commit()
    _use_deterministic_embeddings(monkeypatch)
    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (None, None, None))

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": query},
    )
    events = _events(response)
    answer = "".join(event.get("token", "") for event in events)

    assert response.status_code == 200
    assert "Employee working hours are Monday through Friday, 09:30–18:00 IST" in answer
    assert "flexible core hours from 11:00–16:00 IST" in answer
    assert "HR_and_Operations_Knowledge_Handbook_v2.1.pdf" in answer


def test_indexed_handbook_wins_over_static_policy_fallback(
    db_session: Session,
    employee: Employee,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    _add_indexed_handbook(db_session, employee)
    _use_deterministic_embeddings(monkeypatch)
    static_policy = tmp_path / "company_policy.md"
    static_policy.write_text(
        "Casual leave provides 10 days per year.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", static_policy)

    results = assistant_planner.search_company_knowledge(
        "How many days of casual leave can an employee take?",
        db=db_session,
        actor_id=employee.id,
    )

    assert results
    assert all(
        item["source"] == "HR_and_Operations_Knowledge_Handbook_v2.1.pdf"
        for item in results
    )
    assert any("8 days" in item["snippet"] for item in results)
    assert all("10 days" not in item["snippet"] for item in results)


@pytest.mark.parametrize(
    "query",
    [
        "What are the employee working hours?",
        "What are the working hours?",
        "What does the HR handbook say about employee working hours?",
    ],
)
def test_working_hours_retrieves_the_exact_indexed_handbook_passage(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
):
    _add_indexed_handbook(db_session, employee)
    _use_deterministic_embeddings(monkeypatch)
    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (None, None, None))

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": query},
    )
    events = _events(response)
    answer = "".join(event.get("token", "") for event in events)

    assert response.status_code == 200
    assert "Employee working hours are Monday through Friday, 09:30–18:00 IST" in answer
    assert "flexible core hours from 11:00–16:00 IST" in answer
    assert "Northwind Employee Handbook" not in answer


def test_working_hours_does_not_fall_back_to_static_policy(
    db_session: Session,
    employee: Employee,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    static_policy = tmp_path / "company_policy.md"
    static_policy.write_text(
        "Office hours are 09:00-17:00, according to the legacy policy.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", static_policy)

    results = assistant_planner.search_company_knowledge(
        "What are the working hours?",
        db=db_session,
        actor_id=employee.id,
    )

    assert results == []


@pytest.mark.parametrize(
    "query",
    [
        "What does the HR handbook say about leave?",
        "What does the SOP say about onboarding?",
        "What does the project document say about launch approval?",
    ],
)
def test_chat_uses_semantic_document_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
):
    context = [{"source": "handbook.docx", "snippet": "Verified source passage."}]
    retrieval_calls = []
    generated_context = []

    def retrieve(db, *, actor_id, query):
        retrieval_calls.append((actor_id, query))
        return context

    def generate(messages, **kwargs):
        generated_context.append(kwargs.get("rag_context"))
        return iter(["Grounded answer."])

    monkeypatch.setattr(assistant_planner, "retrieve_semantic_document_context", retrieve)
    monkeypatch.setattr(assistant_service, "stream_chat_completion", generate)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": query},
    )
    events = _events(response)

    assert response.status_code == 200
    assert retrieval_calls == [("semantic-employee", query)]
    assert generated_context == [context]
    assert any("Grounded answer." in event.get("token", "") for event in events)


def test_assistant_retrieves_an_indexed_document_through_semantic_search(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    document = Document(
        id=str(uuid.uuid4()),
        original_filename="hr-handbook.docx",
        stored_filename=f"{uuid.uuid4()}.docx",
        file_size=100,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        owner_id=employee.id,
        status=DOC_STATUS_READY,
        indexing_status=INDEX_STATUS_READY,
        version=3,
        extracted_text=(
            "HR leave procedure states employees submit requests through the HR portal."
        ),
    )
    db_session.add(document)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            id=str(uuid.uuid4()),
            document_id=document.id,
            document_version=document.version,
            chunk_index=0,
            heading="HR Leave Procedure",
            content=document.extracted_text,
            token_count=12,
            department="HR",
            acl_employee_ids=[employee.id],
            embedding=[1.0, 0.0],
            embedding_model=indexing.EMBEDDING_MODEL,
        )
    )
    db_session.commit()
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])
    monkeypatch.setattr(indexing, "embed_passages", lambda passages: [
        [1.0, 0.0] for _ in passages
    ])
    generated_context = []
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda messages, **kwargs: (
            generated_context.append(kwargs.get("rag_context"))
            or iter(["The handbook says to use the HR portal."])
        ),
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "What does the HR leave procedure say?"},
    )
    events = _events(response)

    assert response.status_code == 200
    assert generated_context and generated_context[0]
    assert generated_context[0][0]["source"] == "hr-handbook.docx"
    assert "HR portal" in generated_context[0][0]["snippet"]
    assert any("The handbook says" in event.get("token", "") for event in events)


def test_acl_restricted_document_is_not_retrieved_for_assistant(
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    owner = Employee(
        id="private-document-owner",
        name="Private Owner",
        email="private-owner@northstar.example",
        team="HR",
        is_active=True,
    )
    db_session.add(owner)
    db_session.flush()
    document = Document(
        id=str(uuid.uuid4()),
        original_filename="private-policy.docx",
        stored_filename=f"{uuid.uuid4()}.docx",
        file_size=100,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        owner_id=owner.id,
        status=DOC_STATUS_READY,
        indexing_status=INDEX_STATUS_READY,
        version=2,
        extracted_text="Private policy text.",
    )
    db_session.add(document)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            id=str(uuid.uuid4()),
            document_id=document.id,
            document_version=document.version,
            chunk_index=0,
            heading="Private policy",
            content="Private ORCHID policy details.",
            token_count=4,
            department="HR",
            acl_employee_ids=[owner.id],
            embedding=[1.0, 0.0],
            embedding_model=indexing.EMBEDDING_MODEL,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        indexing,
        "embed_query",
        lambda query: pytest.fail("An inaccessible document must be filtered first."),
    )

    results = retrieve_semantic_document_context(
        db_session,
        actor_id=employee.id,
        query="What are the private HR policy details?",
    )

    assert results == []


def test_shared_with_all_employees_is_semantically_searchable_by_active_employees(
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    document = _add_indexed_handbook(db_session, employee)
    document.shared_with_all_employees = True
    other_employee = Employee(
        id="semantic-active-employee",
        name="Active Employee",
        email="active@northstar.example",
        team="Operations",
        is_active=True,
    )
    inactive_employee = Employee(
        id="semantic-inactive-employee",
        name="Inactive Employee",
        email="inactive@northstar.example",
        team="Operations",
        is_active=False,
    )
    db_session.add_all([other_employee, inactive_employee])
    db_session.commit()
    _use_deterministic_embeddings(monkeypatch)

    active_results = indexing.search_documents(
        db_session,
        actor_id=other_employee.id,
        query="How much casual time off can an employee take?",
    )
    inactive_results = indexing.search_documents(
        db_session,
        actor_id=inactive_employee.id,
        query="How much casual time off can an employee take?",
    )

    assert active_results
    assert active_results[0].document_id == document.id
    assert "8 days" in active_results[0].content
    assert inactive_results == []


def test_email_preview_receives_retrieved_document_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    context = [{
        "source": "project-sop.docx",
        "snippet": "The project launch requires a readiness review.",
    }]
    captured_preview_instructions = []
    captured_document_context = []

    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: context,
    )

    def fake_execute(
        step,
        understanding,
        db,
        user_id,
        *,
        principal=None,
        results=None,
        conversation_id=None,
    ):
        if step.tool_name == "email.preview":
            arguments = prepare_tool_arguments(step, understanding, results, principal)
            captured_preview_instructions.append(arguments.get("instructions", ""))
            captured_document_context.append(arguments.get("document_context", ""))
            step.tool_arguments = arguments
        step.result = {}
        return f"Completed {step.tool_name}."

    monkeypatch.setattr(assistant_router, "execute_plan_step", fake_execute)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Send an email to Diana about the project launch"},
    )

    assert response.status_code == 200
    assert captured_preview_instructions
    assert "project-sop.docx" in captured_preview_instructions[0]
    assert "readiness review" in captured_preview_instructions[0]
    assert captured_document_context == ["The project launch requires a readiness review."]


def test_chat_email_draft_uses_retrieved_handbook_facts(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    _add_indexed_handbook(db_session, employee)
    _use_deterministic_embeddings(monkeypatch)
    recipient = Employee(
        id="semantic-email-recipient",
        name="Rahul Sharma",
        email="rahul.semantic@northstar.example",
        team="HR",
        is_active=True,
    )
    db_session.add(recipient)
    db_session.commit()
    search_calls = []
    real_search_documents = indexing.search_documents

    def track_search_documents(db, *, actor_id, query, limit):
        search_calls.append((actor_id, query, limit))
        return real_search_documents(
            db,
            actor_id=actor_id,
            query=query,
            limit=limit,
        )

    monkeypatch.setattr(indexing, "search_documents", track_search_documents)

    def disable_llm(*args, **kwargs):
        raise RuntimeError("LLM disabled in test")

    monkeypatch.setattr(email_draft_service, "generate_with_llm", disable_llm)
    prompt = "Send an email to Rahul Sharma explaining the company's flexible core hours."

    response = client.post(
        "/api/v1/email-drafts?actor_id=semantic-employee",
        json={
            "prompt": prompt,
            "sender_name": employee.name,
            "sender_email": employee.email,
        },
    )

    assert response.status_code == 201, response.text
    draft = response.json()
    assert search_calls == [(
        employee.id,
        "employee working hours flexible core hours",
        DOCUMENT_SEARCH_RETRIEVAL_LIMIT,
    )]
    assert "Employee working hours are Monday through Friday" in draft["body"]
    assert "flexible core hours from 11:00–16:00 IST" in draft["body"]
    assert "[Add the details you want to share here.]" not in draft["body"]
    assert draft["body"].endswith(f"Best regards,\n{employee.name}")


def test_chat_email_draft_reports_no_relevant_document_context(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [],
    )

    response = client.post(
        "/api/v1/email-drafts?actor_id=semantic-employee",
        json={
            "prompt": "Send an email explaining the company's flexible core hours.",
            "sender_name": employee.name,
            "sender_email": employee.email,
        },
    )

    assert response.status_code == 422
    assert "couldn't find relevant information" in response.json()["detail"]
    assert "didn't create a draft" in response.json()["detail"]
    assert db_session.query(EmailDraft).count() == 0


def test_email_preview_keeps_retrieved_context_out_of_the_email_topic(
    db_session: Session,
    principal: AuthenticatedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
):
    prompts = []
    monkeypatch.setattr(
        email_draft_service,
        "build_recipients",
        lambda *args, **kwargs: ([], [], []),
    )
    monkeypatch.setattr(
        email_draft_service,
        "compose_draft",
        lambda prompt, **kwargs: (
            prompts.append(prompt)
            or {
                "subject": "Project launch",
                "body": "Hello.",
                "source": "template",
                "generation_note": None,
            }
        ),
    )
    monkeypatch.setattr(
        email_draft_service,
        "evaluate_approval",
        lambda recipients: (False, []),
    )
    monkeypatch.setattr(email_draft_service, "scan_sensitive", lambda *args: [])
    tool_context = ToolContext(
        db=db_session,
        principal=principal,
        scopes=frozenset(),
        execution_id="preview-test",
        idempotency_key=None,
        conversation_id=None,
        attempt=1,
        deadline=0,
    )

    assistant_tool_catalog.preview_email(
        tool_context,
        {
            "topic": "project launch",
            "instructions": "Retrieved source: launch readiness requires review.",
        },
    )

    assert "launch readiness requires review" in prompts[0]
    assert email_draft_service.extract_topic(prompts[0]) == "project launch"


@pytest.mark.parametrize("mode", ["template", "auto"])
def test_email_preview_includes_retrieved_facts_and_signs_as_sender(
    db_session: Session,
    principal: AuthenticatedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
):
    fact = "Casual leave entitlement is 8 days per year."
    generated_prompts = []
    monkeypatch.setattr(
        email_draft_service,
        "build_recipients",
        lambda *args, **kwargs: (
            [{"email": "rahul@example.com", "name": "Rahul Recipient", "internal": True}],
            [],
            [],
        ),
    )
    if mode == "auto":
        def generate(prompt, *, sender, greeting):
            generated_prompts.append((prompt, sender, greeting))
            return (
                "Casual leave information",
                "Hi Rahul,\n\nPlease note the leave entitlement.\n\nBest regards,\nRahul",
            )

        monkeypatch.setattr(email_draft_service, "generate_with_llm", generate)
    monkeypatch.setattr(
        email_draft_service,
        "evaluate_approval",
        lambda recipients: (False, []),
    )
    monkeypatch.setattr(email_draft_service, "scan_sensitive", lambda *args: [])
    tool_context = ToolContext(
        db=db_session,
        principal=principal,
        scopes=frozenset(),
        execution_id=f"preview-context-{mode}",
        idempotency_key=None,
        conversation_id=None,
        attempt=1,
        deadline=0,
    )

    preview = assistant_tool_catalog.preview_email(
        tool_context,
        {
            "topic": "casual leave entitlement",
            "document_context": fact,
            "instructions": f"Use the retrieved company-document facts:\n{fact}",
            "mode": mode,
        },
    )

    assert fact in preview["body"]
    assert "[Add the details you want to share here.]" not in preview["body"]
    assert preview["body"].endswith("Best regards,\nDiana Prince")
    assert not preview["body"].endswith("Rahul")
    if mode == "auto":
        assert generated_prompts[0][0].count(fact) >= 1
        assert generated_prompts[0][1] == "Diana Prince"


def test_chat_email_plan_passes_retrieved_facts_and_sender_to_real_preview_tool(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    fact = (
        "Employee working hours are Monday through Friday, 09:30–18:00 IST, "
        "with flexible core hours from 11:00–16:00 IST."
    )
    recipient = Employee(
        id="semantic-rahul",
        name="Rahul Sharma",
        email="rahul@northstar.example",
        team="HR",
        is_active=True,
    )
    db_session.add(recipient)
    db_session.commit()
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [{
            "source": "HR_and_Operations_Knowledge_Handbook_v2.1.pdf",
            "snippet": fact,
            "score": 0.99,
        }],
    )
    monkeypatch.setattr(
        assistant_planner,
        "assistant_email_draft_mode",
        lambda: "template",
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={
            "message": (
                "Send an email to Rahul explaining the company's flexible core hours "
                "according to the HR handbook."
            )
        },
    )
    events = _events(response)
    awaiting = next(
        event["awaiting_confirmation"]
        for event in events
        if "awaiting_confirmation" in event
    )
    body = awaiting["arguments"]["body"]

    assert response.status_code == 200
    assert awaiting["arguments"]["subject"]
    assert "email" not in awaiting["arguments"]["subject"].casefold()
    assert "flexible core hours" in awaiting["arguments"]["subject"].casefold()
    assert any(fact in event.get("retrieval", {}).get("snippet", "") for event in events)
    assert fact in body
    assert "[Add the details you want to share here.]" not in body
    assert body.endswith(f"Best regards,\n{employee.name}")
    assert not body.endswith(f"Best regards,\n{recipient.name}")

    confirmed_response = client.post(
        "/api/v1/assistant/chat/stream",
        json={
            "message": "confirm",
            "conversation_id": events[0]["conversation_id"],
        },
    )
    confirmed_events = _events(confirmed_response)
    draft = db_session.query(EmailDraft).one()

    assert confirmed_response.status_code == 200
    assert any("step_done" in event for event in confirmed_events)
    assert fact in draft.body
    assert "[Add the details you want to share here.]" not in draft.body
    assert draft.body.endswith(f"Best regards,\n{employee.name}")
    assert draft.created_by_name == employee.name
    assert draft.status == "queued"


def test_chat_send_step_repairs_legacy_preview_body_before_confirmation(
    client: TestClient,
    db_session: Session,
    employee: Employee,
    monkeypatch: pytest.MonkeyPatch,
):
    fact = "Casual Leave: Employees may take up to 8 days per calendar year."
    recipient = Employee(
        id="semantic-rahul-legacy",
        name="Rahul Sharma",
        email="rahul.legacy@northstar.example",
        team="HR",
        is_active=True,
    )
    db_session.add(recipient)
    db_session.commit()
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [{
            "source": "HR_and_Operations_Knowledge_Handbook_v2.1.pdf",
            "snippet": fact,
            "score": 0.99,
        }],
    )
    monkeypatch.setattr(
        assistant_planner,
        "assistant_email_draft_mode",
        lambda: "template",
    )
    monkeypatch.setattr(
        email_draft_service,
        "compose_draft",
        lambda *args, **kwargs: {
            "source": "template",
            "template_key": "general",
            "subject": "Casual leave",
            "body": (
                "Hi Rahul,\n\n"
                "[Add the details you want to share here.]\n\n"
                "Best regards,\nRahul"
            ),
            "generation_note": None,
        },
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Send an email to Rahul about casual leave using the HR handbook"},
    )
    events = _events(response)
    awaiting = next(event["awaiting_confirmation"] for event in events if "awaiting_confirmation" in event)
    body = awaiting["arguments"]["body"]

    assert response.status_code == 200
    assert fact in body
    assert "[Add the details you want to share here.]" not in body
    assert body.endswith(f"Best regards,\n{employee.name}")
    assert recipient.name not in body.rsplit("Best regards,\n", 1)[-1]

    confirmed_response = client.post(
        "/api/v1/assistant/chat/stream",
        json={
            "message": "confirm",
            "conversation_id": events[0]["conversation_id"],
        },
    )
    saved_draft = db_session.query(EmailDraft).one()

    assert confirmed_response.status_code == 200
    assert fact in saved_draft.body
    assert "[Add the details you want to share here.]" not in saved_draft.body
    assert saved_draft.body.endswith(f"Best regards,\n{employee.name}")
    assert saved_draft.created_by_name == employee.name
    assert saved_draft.status == "queued"


def test_email_plan_reports_when_no_relevant_document_context_exists(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [],
    )
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_company_knowledge",
        lambda query, *, db, actor_id: None,
    )

    def fake_execute(step, *args, **kwargs):
        step.result = {}
        return f"Completed {step.tool_name}."

    monkeypatch.setattr(assistant_router, "execute_plan_step", fake_execute)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Send an email to Diana about the project update"},
    )
    tokens = "".join(event.get("token", "") for event in _events(response))

    assert response.status_code == 200
    assert "couldn't find relevant information" in tokens


def test_email_plan_does_not_report_missing_context_after_plan_step_retrieval(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    snippet = "The HR handbook provides 8 days of casual leave."
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [],
    )
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_company_knowledge",
        lambda query, *, db, actor_id: snippet,
    )

    def fake_execute(step, *args, **kwargs):
        step.result = {}
        return f"Completed {step.tool_name}."

    monkeypatch.setattr(assistant_router, "execute_plan_step", fake_execute)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Send an email to Diana about casual leave from the HR handbook"},
    )
    events = _events(response)
    tokens = "".join(event.get("token", "") for event in events)

    assert response.status_code == 200
    assert any(snippet in event.get("retrieval", {}).get("snippet", "") for event in events)
    assert "Done." in tokens
    assert "couldn't find relevant information" not in tokens


def test_document_grounded_plan_uses_retrieved_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    context = [{"source": "onboarding-sop.docx", "snippet": "Use the onboarding checklist."}]
    received_context = []

    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: context,
    )

    def generate(messages, **kwargs):
        received_context.append(kwargs.get("rag_context"))
        return iter(["Plan based on the onboarding checklist."])

    monkeypatch.setattr(assistant_service, "stream_chat_completion", generate)

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "Create an onboarding plan based on the HR SOP."},
    )
    events = _events(response)

    assert response.status_code == 200
    assert not any("plan" in event for event in events)
    assert received_context == [context]
    assert any(
        "Plan based on the onboarding checklist." in event.get("token", "")
        for event in events
    )


def test_no_relevant_document_context_returns_not_found_without_model_answer(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        assistant_planner,
        "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [],
    )
    monkeypatch.setattr(
        assistant_service,
        "stream_chat_completion",
        lambda *args, **kwargs: pytest.fail(
            "The model must not answer a document question without relevant context."
        ),
    )

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"message": "What does the HR policy say about remote work?"},
    )
    events = _events(response)
    answer = "".join(event.get("token", "") for event in events)

    assert "couldn't find relevant information" in answer
    assert "indexed company documents" in answer
