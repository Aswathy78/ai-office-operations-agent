"""Story 7.4 — document retrieval respects access control.

Coverage
--------
Unauthorized  An employee asking about an HR-restricted document gets a safe answer:
              no content, no citation, nothing that reveals the document exists.
Authorized    A manager on the document's ACL gets a grounded answer citing it.
Before rank   Unreadable chunks never reach ranking or the embedding of passages.
Answer time   Retrieved or injected context is re-checked before use; a revoked
              grant, a stale version or an unverifiable item is dropped; errors deny.
Secure dflt   Missing chunk ACL metadata, inactive or unknown users -> no access.
Leakage       Mixed context cites only the documents the user may read.
Workflows     Assistant e-mail: facts from documents the sender can no longer read
              stop the send at confirmation; quoting a restricted document to people
              outside its ACL is high risk. Chatbox drafts re-check at send / retry.
"""

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
from app import email_providers
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.documents import indexing
from app.documents import service as document_service
from app.documents.models import (
    DOC_STATUS_READY,
    INDEX_STATUS_READY,
    Document,
    DocumentACLEntry,
    DocumentChunk,
)
from app.email_draft_service import (
    EmailActor,
    EmailDraftConflict,
    create_draft_from_chat,
    retry_draft,
    send_draft,
)
from app.meeting.models import AssistantActionApproval, Base, EmailDraft, Employee

RESTRICTED_FILE = "HR-Salary-Bands-Confidential.pdf"
RESTRICTED_FACT = "Salary band L5: INR 30 lakh per year."
RESTRICTED_TEXT = f"# Salary Bands\n\n{RESTRICTED_FACT}"
SHARED_FILE = "Employee-Handbook.pdf"
SHARED_TEXT = "# Working Hours\n\nEmployee working hours are Monday through Friday, 09:30–18:00 IST."
QUESTION = "What does the HR policy say about the salary band for L5?"

PEOPLE = {
    # id: (name, email, team, role, active)
    "acl-hr": ("Hana Reyes", "hana@northstar.example", "HR", "hr_operations", True),
    "acl-mgr": ("Mark Rivera", "mark@northstar.example", "Engineering", "manager", True),
    "acl-emp": ("Evan Cole", "evan@northstar.example", "Engineering", "employee", True),
    "acl-zoe": ("Zoe Kim", "zoe@northstar.example", "Engineering", "employee", True),
    "acl-gone": ("Gil Former", "gil@northstar.example", "HR", "employee", False),
}


class FakeMail:
    def __call__(self, recipient, subject, body):
        return email_providers.DeliveryReceipt("<id@provider.example>", "fake")


@pytest.fixture(autouse=True)
def _mail(monkeypatch):
    monkeypatch.setattr(email_providers, "send_email", FakeMail())


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    for employee_id, (name, email, team, _, active) in PEOPLE.items():
        session.add(Employee(
            id=employee_id, name=name, email=email, team=team, is_active=active,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            working_hours_start="00:00", working_hours_end="23:59",
        ))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _person(employee_id: str) -> AuthenticatedPrincipal:
    name, email, team, role, _ = PEOPLE[employee_id]
    return AuthenticatedPrincipal(
        user_id=employee_id, account_id=f"acc-{employee_id}", email=email, name=name,
        role=role, team=team, jti=f"jti-{employee_id}",
    )


@pytest.fixture()
def client(db_session: Session):
    current = {"principal": _person("acl-emp")}
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: current["principal"]
    with TestClient(app) as test_client:
        test_client.login = lambda employee_id: current.update(principal=_person(employee_id))
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _deterministic_embeddings(monkeypatch):
    """Equal similarity everywhere, so only access rules and wording decide."""
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])
    monkeypatch.setattr(indexing, "embed_passages", lambda passages: [[1.0, 0.0] for _ in passages])


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setattr(assistant_service, "get_llm_client", lambda: (None, None, None))


def _document(
    db: Session,
    *,
    filename: str,
    text: str,
    owner_id: str = "acl-hr",
    acl: tuple[str, ...] = (),
    shared: bool = False,
    chunk_acl: list[str] | None = None,
    version: int = 1,
) -> Document:
    document = Document(
        id=str(uuid.uuid4()), original_filename=filename, stored_filename=f"{uuid.uuid4()}.pdf",
        file_size=100, mime_type="application/pdf", owner_id=owner_id,
        status=DOC_STATUS_READY, indexing_status=INDEX_STATUS_READY, version=version,
        extracted_text=text, shared_with_all_employees=shared,
    )
    db.add(document)
    db.flush()
    for employee_id in acl:
        db.add(DocumentACLEntry(id=str(uuid.uuid4()), document_id=document.id, employee_id=employee_id))
    db.add(DocumentChunk(
        id=str(uuid.uuid4()), document_id=document.id, document_version=version, chunk_index=0,
        heading=None, content=text, token_count=20, department="HR",
        acl_employee_ids=chunk_acl if chunk_acl is not None else [owner_id, *acl],
        embedding=[1.0, 0.0], embedding_model=indexing.EMBEDDING_MODEL,
    ))
    db.commit()
    return document


def _restricted(db: Session, *extra_acl: str) -> Document:
    """HR-owned salary bands, readable by the HR owner and the manager on its ACL."""
    return _document(db, filename=RESTRICTED_FILE, text=RESTRICTED_TEXT, acl=("acl-mgr", *extra_acl))


def _item(document: Document, snippet: str, version: int | None = None) -> dict:
    return {
        "source": document.original_filename,
        "snippet": snippet,
        "document_id": document.id,
        "document_version": version if version is not None else document.version,
    }


def _ask(client: TestClient, message: str, conversation_id: str | None = None):
    body = {"message": message, **({"conversation_id": conversation_id} if conversation_id else {})}
    response = client.post("/api/v1/assistant/chat/stream", json=body)
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]
    return response.text, events


def _answer(events: list[dict]) -> str:
    return "".join(event.get("token", "") for event in events)


def _no_model(monkeypatch):
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda *args, **kwargs: pytest.fail("Restricted context must never reach the model."),
    )


def _assert_no_leak(raw: str) -> None:
    for secret in (RESTRICTED_FILE, "Salary-Bands", "30 lakh", RESTRICTED_FACT):
        assert secret not in raw, f"leaked {secret!r}"


# ---------------------------------------------------------------------------
# Unauthorized employee / authorized manager
# ---------------------------------------------------------------------------


def test_unauthorized_employee_gets_a_safe_answer_that_reveals_nothing(
    client: TestClient, db_session: Session, monkeypatch,
):
    _restricted(db_session)
    _no_model(monkeypatch)

    raw, events = _ask(client, QUESTION)

    assert _answer(events) == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    assert not any("retrieval" in event for event in events)
    _assert_no_leak(raw)


def test_authorized_manager_gets_a_grounded_answer_with_a_valid_citation(
    client: TestClient, db_session: Session, no_llm,
):
    _restricted(db_session)
    client.login("acl-mgr")

    _, events = _ask(client, QUESTION)

    answer = _answer(events)
    assert RESTRICTED_FACT in answer
    assert f"[1] {RESTRICTED_FILE}" in answer
    [retrieval] = [event["retrieval"] for event in events if "retrieval" in event]
    assert retrieval["sources"] == [RESTRICTED_FILE]


def test_authorized_manager_citation_reaches_the_model_as_grounding(
    client: TestClient, db_session: Session, monkeypatch,
):
    _restricted(db_session)
    client.login("acl-mgr")
    received = []
    monkeypatch.setattr(
        assistant_service, "stream_chat_completion",
        lambda messages, **kwargs: received.append(kwargs["rag_context"]) or iter(["ok"]),
    )

    _ask(client, QUESTION)

    [context] = received
    assert [item["source"] for item in context] == [RESTRICTED_FILE]
    assert RESTRICTED_FACT in context[0]["snippet"]


# ---------------------------------------------------------------------------
# ACL filtering before ranking
# ---------------------------------------------------------------------------


def test_unreadable_chunks_never_reach_ranking(db_session: Session, monkeypatch):
    _restricted(db_session)
    _document(db_session, filename=SHARED_FILE, text=SHARED_TEXT, shared=True)
    ranked_passages = []
    monkeypatch.setattr(
        indexing, "embed_passages",
        lambda passages: [ranked_passages.append(p) or [1.0, 0.0] for p in passages],
    )

    results = indexing.search_documents(
        db_session, actor_id="acl-emp", query="salary band L5 working hours", limit=8,
    )

    assert ranked_passages, "the readable document is still searched"
    assert all("Salary" not in passage for passage in ranked_passages)
    assert all(result.filename != RESTRICTED_FILE for result in results)


def test_the_restricted_document_is_found_for_its_acl_members(db_session: Session):
    document = _restricted(db_session)
    for reader in ("acl-hr", "acl-mgr"):
        results = indexing.search_documents(
            db_session, actor_id=reader, query="salary band L5", limit=8,
        )
        assert [result.document_id for result in results] == [document.id]
    assert indexing.search_documents(
        db_session, actor_id="acl-emp", query="salary band L5", limit=8,
    ) == []


# ---------------------------------------------------------------------------
# Permission re-check at answer time
# ---------------------------------------------------------------------------


def test_context_injected_from_another_workflow_is_rechecked(
    client: TestClient, db_session: Session, monkeypatch,
):
    document = _restricted(db_session)
    monkeypatch.setattr(
        assistant_planner, "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [_item(document, RESTRICTED_FACT)],
    )
    _no_model(monkeypatch)

    raw, events = _ask(client, QUESTION)

    assert _answer(events) == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    _assert_no_leak(raw)


def test_permission_revoked_between_retrieval_and_answer(
    client: TestClient, db_session: Session, monkeypatch,
):
    document = _restricted(db_session, "acl-emp")
    real_retrieve = assistant_planner.retrieve_semantic_document_context

    def retrieve_then_revoke(db, *, actor_id, query):
        context = real_retrieve(db, actor_id=actor_id, query=query)
        assert context, "the employee could read it at retrieval time"
        db.query(DocumentACLEntry).filter_by(document_id=document.id, employee_id="acl-emp").delete()
        db.commit()
        return context

    monkeypatch.setattr(assistant_planner, "retrieve_semantic_document_context", retrieve_then_revoke)
    _no_model(monkeypatch)

    raw, events = _ask(client, QUESTION)

    assert _answer(events) == assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
    _assert_no_leak(raw)


def test_stale_and_unverifiable_context_is_dropped(db_session: Session):
    readable = _document(db_session, filename=SHARED_FILE, text=SHARED_TEXT, shared=True)
    context = [
        _item(readable, "Current text."),
        _item(readable, "Text from an older version.", version=readable.version - 1 or 99),
        {"source": "made-up.pdf", "snippet": "No document behind this."},
        {"source": "company_policy.md", "snippet": "Not actually in the policy file."},
    ]

    kept = assistant_planner.authorize_document_context(
        db_session, actor_id="acl-emp", context=context
    )

    assert kept == [context[0]]


def test_recheck_fails_closed_when_permissions_cannot_be_verified(db_session: Session, monkeypatch):
    readable = _document(db_session, filename=SHARED_FILE, text=SHARED_TEXT, shared=True)

    def broken(actor_id):
        raise RuntimeError("permission store unavailable")

    monkeypatch.setattr(document_service, "_document_access_filter", broken)

    assert assistant_planner.authorize_document_context(
        db_session, actor_id="acl-emp", context=[_item(readable, "Current text.")]
    ) == []


# ---------------------------------------------------------------------------
# Secure defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk_acl", [[], None])
def test_chunks_missing_acl_metadata_are_never_candidates(db_session: Session, chunk_acl):
    document = _document(
        db_session, filename=SHARED_FILE, text=SHARED_TEXT, owner_id="acl-emp",
        chunk_acl=chunk_acl if chunk_acl is not None else [],
    )
    if chunk_acl is None:
        chunk = db_session.query(DocumentChunk).filter_by(document_id=document.id).one()
        chunk.acl_employee_ids = None
        db_session.commit()

    assert indexing.search_documents(
        db_session, actor_id="acl-emp", query="working hours", limit=8,
    ) == []


@pytest.mark.parametrize("actor_id", ["acl-gone", "nobody-at-all"])
def test_inactive_or_unknown_people_read_nothing(db_session: Session, actor_id: str):
    document = _restricted(db_session, "acl-gone")
    _document(db_session, filename=SHARED_FILE, text=SHARED_TEXT, shared=True)

    assert indexing.search_documents(db_session, actor_id=actor_id, query="salary band", limit=8) == []
    assert document_service.accessible_document_versions(db_session, actor_id, {document.id}) == {}


# ---------------------------------------------------------------------------
# Citation leakage
# ---------------------------------------------------------------------------


def test_mixed_context_cites_only_documents_the_user_may_read(
    client: TestClient, db_session: Session, no_llm, monkeypatch,
):
    restricted = _restricted(db_session)
    readable = _document(db_session, filename=SHARED_FILE, text=SHARED_TEXT, shared=True)
    monkeypatch.setattr(
        assistant_planner, "retrieve_semantic_document_context",
        lambda db, *, actor_id, query: [
            _item(readable, "Employee working hours are 09:30–18:00 IST."),
            _item(restricted, RESTRICTED_FACT),
        ],
    )

    raw, events = _ask(client, "What are the working hours in the HR policy?")

    assert SHARED_FILE in _answer(events)
    _assert_no_leak(raw)


# ---------------------------------------------------------------------------
# E-mail workflows
# ---------------------------------------------------------------------------


def test_revoked_access_stops_an_assistant_email_at_confirmation(
    client: TestClient, db_session: Session,
):
    # Not sensitive data, so the e-mail is medium risk and waits for the sender.
    launch_fact = "The Falcon project launch is on 14 November 2026."
    document = _document(
        db_session, filename="Falcon-Launch-Plan-Restricted.pdf",
        text="# Launch\n\n" + launch_fact, acl=("acl-emp", "acl-zoe"),
    )
    _, events = _ask(client, "Send an email to Zoe about the Falcon project launch")
    [waiting] = [event["awaiting_confirmation"] for event in events if "awaiting_confirmation" in event]
    assert waiting["arguments"]["document_ids"] == [document.id]
    assert launch_fact in waiting["arguments"]["body"]

    # The employee loses access before confirming.
    db_session.query(DocumentACLEntry).filter_by(document_id=document.id, employee_id="acl-emp").delete()
    db_session.commit()
    raw, events = _ask(client, "confirm", events[0]["conversation_id"])

    assert db_session.query(EmailDraft).count() == 0
    assert "no longer have access" in _answer(events)
    assert "Falcon-Launch-Plan-Restricted.pdf" not in _answer(events)
    approval = db_session.get(AssistantActionApproval, waiting["approval_id"])
    assert approval.status == "failed"


def test_quoting_a_restricted_document_to_people_outside_its_acl_is_high_risk(
    client: TestClient, db_session: Session,
):
    _restricted(db_session, "acl-emp")  # Zoe is not on the ACL.

    _, events = _ask(client, "Send an email to Zoe about the salary band policy")

    [paused] = [event["approval_required"] for event in events if "approval_required" in event]
    assert paused["risk_level"] == "high"
    assert any("outside its access list" in reason for reason in paused["risk_reasons"])
    assert db_session.query(EmailDraft).count() == 0


def test_chatbox_draft_is_not_sent_or_retried_after_access_is_revoked(db_session: Session):
    document = _restricted(db_session, "acl-emp")
    actor = EmailActor(id="acl-emp", role="employee", team="Engineering")
    draft = create_draft_from_chat(
        db_session, actor, prompt="Email Zoe about the salary bands", sender_name="Evan Cole",
        sender_email="evan@northstar.example", mode="template",
        retrieved_context=RESTRICTED_FACT, source_document_ids=[document.id],
    )
    db_session.query(DocumentACLEntry).filter_by(document_id=document.id, employee_id="acl-emp").delete()
    db_session.commit()

    with pytest.raises(EmailDraftConflict, match="no longer have access") as denied:
        send_draft(db_session, draft.id, actor, confirm=True, acknowledge_sensitive=True)
    assert RESTRICTED_FILE not in str(denied.value)
    assert db_session.get(EmailDraft, draft.id).status == "draft"

    draft.status = "failed"
    db_session.commit()
    with pytest.raises(EmailDraftConflict, match="no longer have access"):
        retry_draft(db_session, draft.id, actor)


def test_chatbox_route_uses_only_documents_the_sender_may_read(
    client: TestClient, db_session: Session,
):
    _restricted(db_session)  # The employee is not on the ACL.

    response = client.post(
        "/api/v1/email-drafts?actor_id=acl-emp",
        json={
            "prompt": "Send an email to Zoe explaining the salary band policy according to the HR handbook.",
            "sender_name": "Evan Cole",
            "sender_email": "evan@northstar.example",
        },
    )

    assert response.status_code == 422
    assert "couldn't find relevant information" in response.json()["detail"]
    assert RESTRICTED_FILE not in response.text and "30 lakh" not in response.text
    assert db_session.query(EmailDraft).count() == 0


# --- Editing access without replacing the file ---------------------------------


def _acl_ids(db_session: Session, document: Document) -> set[str]:
    db_session.expire_all()
    return {
        row.employee_id
        for row in db_session.query(DocumentACLEntry).filter(DocumentACLEntry.document_id == document.id)
    }


def _chunk_acl(db_session: Session, document: Document) -> list[str]:
    db_session.expire_all()
    return db_session.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).one().acl_employee_ids


def test_access_can_be_edited_without_a_replacement_file(client: TestClient, db_session: Session):
    document = _restricted(db_session)
    stored = document.stored_filename
    client.login("acl-hr")

    response = client.put(
        f"/api/v1/documents/{document.id}",
        data={"acl_employee_ids": json.dumps(["acl-emp", "acl-zoe"])},
    )

    assert response.status_code == 200, response.text
    assert _acl_ids(db_session, document) == {"acl-emp", "acl-zoe"}
    # The file, version and index are untouched; the search chunk follows the new list.
    db_session.refresh(document)
    assert document.version == 1 and document.stored_filename == stored
    assert document.indexing_status == INDEX_STATUS_READY
    assert sorted(_chunk_acl(db_session, document)) == ["acl-emp", "acl-hr", "acl-zoe"]


def test_access_edit_changes_what_people_can_find_immediately(client: TestClient, db_session: Session):
    document = _restricted(db_session)
    assert indexing.search_documents(db_session, actor_id="acl-emp", query="salary band L5", limit=5) == []
    client.login("acl-hr")

    client.put(f"/api/v1/documents/{document.id}", data={"acl_employee_ids": json.dumps(["acl-emp"])})
    found = indexing.search_documents(db_session, actor_id="acl-emp", query="salary band L5", limit=5)
    assert [hit.document_id for hit in found] == [document.id]

    client.put(f"/api/v1/documents/{document.id}", data={"acl_employee_ids": json.dumps([])})
    assert _acl_ids(db_session, document) == set()
    assert indexing.search_documents(db_session, actor_id="acl-emp", query="salary band L5", limit=5) == []
    assert indexing.search_documents(db_session, actor_id="acl-hr", query="salary band L5", limit=5)


def test_sharing_with_everyone_can_be_toggled_without_a_file(client: TestClient, db_session: Session):
    document = _restricted(db_session)
    client.login("acl-hr")

    response = client.put(f"/api/v1/documents/{document.id}", data={"shared_with_all_employees": "true"})

    assert response.status_code == 200, response.text
    db_session.refresh(document)
    assert document.shared_with_all_employees is True
    assert _acl_ids(db_session, document) == {"acl-mgr"}


def test_unknown_people_are_skipped_with_a_warning_when_editing_access(client: TestClient, db_session: Session):
    document = _restricted(db_session)
    client.login("acl-hr")

    response = client.put(
        f"/api/v1/documents/{document.id}",
        data={"acl_employee_ids": json.dumps(["acl-zoe", "acl-gone", "nobody"])},
    )

    assert response.status_code == 200
    assert _acl_ids(db_session, document) == {"acl-zoe"}
    assert len(response.json()["warnings"]) == 2


def test_an_edit_that_changes_nothing_is_rejected(client: TestClient, db_session: Session):
    document = _restricted(db_session)
    client.login("acl-hr")

    assert client.put(f"/api/v1/documents/{document.id}").status_code == 422
    assert _acl_ids(db_session, document) == {"acl-mgr"}


def test_ordinary_employees_cannot_edit_access(client: TestClient, db_session: Session):
    document = _restricted(db_session)
    client.login("acl-emp")

    response = client.put(
        f"/api/v1/documents/{document.id}", data={"acl_employee_ids": json.dumps(["acl-emp"])},
    )

    assert response.status_code == 403
    assert _acl_ids(db_session, document) == {"acl-mgr"}
