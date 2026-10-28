"""Focused Story 7.2 chunking, vector persistence, and retry tests."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.documents.chunking import chunk_document_text
from app.documents.indexing import index_document, search_documents
from app.documents.models import (
    DOC_STATUS_READY,
    INDEX_STATUS_FAILED,
    INDEX_STATUS_PENDING,
    INDEX_STATUS_PROCESSING,
    INDEX_STATUS_READY,
    Document,
    DocumentACLEntry,
    DocumentChunk,
)
from app.documents.service import queue_document_reindex, retry_document_index
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
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _word_token_count(text: str) -> int:
    return len(text.split())


def _ready_document(db: Session, *, text: str, shared_with: str | None = None) -> Document:
    owner = Employee(
        id="index-owner",
        name="Index Owner",
        email="index-owner@example.test",
        is_active=True,
        team="Operations",
    )
    db.add(owner)
    if shared_with:
        db.add(
            Employee(
                id=shared_with,
                name="Shared Viewer",
                email=f"{shared_with}@example.test",
                is_active=True,
                team="HR",
            )
        )
    db.flush()
    doc = Document(
        id=str(uuid.uuid4()),
        original_filename="handbook.docx",
        stored_filename=f"{uuid.uuid4()}.docx",
        file_size=100,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        owner_id=owner.id,
        status=DOC_STATUS_READY,
        indexing_status=INDEX_STATUS_PENDING,
        version=3,
        extracted_text=text,
    )
    db.add(doc)
    if shared_with:
        db.add(
            DocumentACLEntry(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                employee_id=shared_with,
            )
        )
    db.commit()
    return doc


def test_heading_aware_chunks_respect_token_limit_and_overlap():
    source = "# Travel Policy\n\n" + " ".join(
        f"policyword{i}" for i in range(1600)
    )
    chunks = chunk_document_text(source, _word_token_count)

    assert len(chunks) >= 3
    assert all(0 < chunk.token_count <= 800 for chunk in chunks)
    assert all(chunk.heading == "Travel Policy" for chunk in chunks)
    assert all(chunk.content.startswith("Travel Policy\n\n") for chunk in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        assert previous.content.split()[-100:] == following.content.split()[2:102]


def test_short_heading_sections_are_packed_to_target_with_boundary_overlap():
    source = "\n\n".join(
        f"## Section {section}\n\n"
        + " ".join(f"section{section}word{word}" for word in range(40))
        for section in range(30)
    )
    chunks = chunk_document_text(source, _word_token_count)

    assert len(chunks) == 3
    assert all(0 < chunk.token_count <= 800 for chunk in chunks)
    assert all(500 <= chunk.token_count <= 800 for chunk in chunks[:-1])
    assert "Section 0" in chunks[0].content
    assert "Section 29" in chunks[-1].content
    assert len(chunks[0].heading.split(" | ")) > 1

    for previous, following in zip(chunks, chunks[1:]):
        previous_words = previous.content.split()
        following_words = following.content.split()
        overlap = max(
            (
                count
                for count in range(
                    1,
                    min(len(previous_words), len(following_words)) + 1,
                )
                if previous_words[-count:] == following_words[:count]
            ),
            default=0,
        )
        assert overlap == 100


def test_empty_document_produces_no_chunks():
    assert chunk_document_text(" \n\t", _word_token_count) == []


def test_semantic_search_returns_all_bullets_as_one_passage(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    bullets = (
        "• First key point: align the text and image representations.\n"
        "• Second key point: compute attention across modalities.\n"
        "• Third key point: combine the attended features.\n"
        "• Fourth key point: preserve the relevant context.\n"
        "Input | Output\n"
        "Image | Visual features"
    )
    doc = _ready_document(
        db_session,
        text=f"# Cross-modal attention\n\n{bullets}",
    )
    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: [[1.0, 0.0] for _ in passages],
    )
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])

    assert index_document(db_session, doc.id)
    results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="What are the four key points of cross-modal attention?",
    )

    assert results
    answer = results[0].content
    assert all(
        f"• {ordinal} key point" in answer
        for ordinal in ("First", "Second", "Third", "Fourth")
    )
    assert results[0].heading == "Cross-modal attention"
    table_results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="What output is produced from image input?",
    )
    assert any("Image | Visual features" in result.content for result in table_results)


def test_search_ranks_semantic_body_over_coarse_chunk_and_heading_matches(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    doc = _ready_document(db_session, text="Policy index.")
    doc.indexing_status = INDEX_STATUS_READY
    passages = (
        (
            "Casual Leave",
            "Casual leave provides 8 days per calendar year with no carry forward.",
            [1.0, 0.0],
        ),
        (
            "Earned Leave",
            "Earned leave provides 18 days per year with up to 30 days carried forward.",
            [0.6, 0.8],
        ),
        (
            "Annual Leave",
            "This section introduces the company's time-off policies.",
            [1.0, 0.0],
        ),
    )
    for index, (heading, content, embedding) in enumerate(passages):
        db_session.add(
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                document_version=doc.version,
                chunk_index=index,
                heading=heading,
                content=content,
                token_count=_word_token_count(content),
                department="Operations",
                acl_employee_ids=[doc.owner_id],
                embedding=embedding,
                embedding_model=indexing.EMBEDDING_MODEL,
            )
        )
    db_session.commit()
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])

    def passage_vectors(passages):
        vectors = []
        for passage in passages:
            content = passage.casefold()
            if "18 days" in content:
                vectors.append([0.86, 0.51])
            elif "8 days" in content:
                vectors.append([0.83, 0.56])
            elif "annual leave" in content:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors

    monkeypatch.setattr(indexing, "embed_passages", passage_vectors)
    results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="annual leave",
        limit=5,
    )

    assert results
    assert results[0].heading == "Earned Leave"
    assert "18 days" in results[0].content
    assert all(result.heading != "Annual Leave" for result in results)


def test_search_collapses_overlapping_passages_but_keeps_distinct_relevant_content(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    doc = _ready_document(db_session, text="Resume index.")
    doc.indexing_status = INDEX_STATUS_READY
    passages = {
        "overlap-a": (
            "Professional Summary",
            "Professional summary: Senior operations analyst with experience in "
            "process improvement, executive reporting, stakeholder engagement, "
            "project coordination, and team leadership.",
        ),
        "overlap-b": (
            "Professional Summary",
            "Professional summary: Senior operations analyst with experience in "
            "process improvement, executive reporting, stakeholder engagement, "
            "project coordination, and team leadership across multiple regions.",
        ),
        "distinct": (
            "Professional Summary",
            "Professional summary: Led digital transformation and vendor governance "
            "across international operations.",
        ),
    }
    for index, content in enumerate(passages):
        db_session.add(
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                document_version=doc.version,
                chunk_index=index,
                heading="Professional Summary",
                content=content,
                token_count=_word_token_count(content),
                department="Operations",
                acl_employee_ids=[doc.owner_id],
                embedding=[1.0, 0.0],
                embedding_model=indexing.EMBEDDING_MODEL,
            )
        )
    db_session.commit()
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])
    monkeypatch.setattr(
        indexing,
        "_search_passages",
        lambda content: [passages[content]],
    )
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda contents: [[1.0, 0.0] for _ in contents],
    )

    results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="professional summary",
        limit=5,
    )

    assert len(results) == 2
    assert any("across multiple regions" in result.content for result in results)
    assert any("digital transformation" in result.content for result in results)


def test_empty_extracted_text_is_reported_as_index_failure(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: pytest.fail("Empty documents must not be embedded."),
    )
    doc = _ready_document(db_session, text=" \n\t")

    assert not index_document(db_session, doc.id)
    db_session.refresh(doc)
    assert doc.indexing_status == INDEX_STATUS_FAILED
    assert "no text" in doc.indexing_failure_reason


def test_index_persists_acl_department_version_and_is_idempotent(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: [[1.0, 0.0] for _ in passages],
    )
    doc = _ready_document(
        db_session,
        text="# Benefits\n\nHealth and dental coverage information.",
        shared_with="index-viewer",
    )

    assert index_document(db_session, doc.id)
    db_session.refresh(doc)
    chunks = db_session.query(DocumentChunk).filter_by(document_id=doc.id).all()
    assert doc.indexing_status == INDEX_STATUS_READY
    assert len(chunks) == 1
    assert chunks[0].document_version == 3
    assert chunks[0].department == "Operations"
    assert chunks[0].acl_employee_ids == ["index-owner", "index-viewer"]
    assert chunks[0].heading == "Benefits"
    assert chunks[0].embedding == [1.0, 0.0]
    assert not index_document(db_session, doc.id)
    assert db_session.query(DocumentChunk).filter_by(document_id=doc.id).count() == 1


def test_reindex_rebuild_is_atomic_and_preserves_version_and_acl(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: [[0.0, 1.0] for _ in passages],
    )
    doc = _ready_document(
        db_session,
        text="# Benefits\n\n" + " ".join(f"benefit{i}" for i in range(600)),
        shared_with="index-viewer",
    )
    doc.indexing_status = INDEX_STATUS_READY
    old_chunk = DocumentChunk(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        document_version=doc.version,
        chunk_index=0,
        heading="Old heading layout",
        content="old compact chunk",
        token_count=3,
        department="Operations",
        acl_employee_ids=["index-owner", "index-viewer"],
        embedding=[1.0, 0.0],
        embedding_model=indexing.EMBEDDING_MODEL,
    )
    db_session.add(old_chunk)
    db_session.commit()
    old_chunk_id = old_chunk.id
    original_version = doc.version

    assert queue_document_reindex(db_session, doc)
    db_session.refresh(doc)
    assert doc.indexing_status == INDEX_STATUS_PENDING
    assert doc.version == original_version
    assert (
        db_session.query(DocumentChunk).filter_by(document_id=doc.id).one().id
        == old_chunk_id
    )

    assert index_document(db_session, doc.id)
    db_session.refresh(doc)
    new_chunks = (
        db_session.query(DocumentChunk)
        .filter_by(document_id=doc.id)
        .order_by(DocumentChunk.chunk_index)
        .all()
    )
    assert doc.indexing_status == INDEX_STATUS_READY
    assert doc.version == original_version
    assert len(new_chunks) == 1
    assert new_chunks[0].id != old_chunk_id
    assert new_chunks[0].document_version == original_version
    assert new_chunks[0].token_count >= 500
    assert new_chunks[0].department == "Operations"
    assert new_chunks[0].acl_employee_ids == ["index-owner", "index-viewer"]
    assert new_chunks[0].embedding == [0.0, 1.0]


def test_failed_reindex_keeps_previous_chunks_and_allows_safe_retry(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    doc = _ready_document(
        db_session,
        text="Updated source content for a retry.",
    )
    doc.indexing_status = INDEX_STATUS_READY
    old_chunk = DocumentChunk(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        document_version=doc.version,
        chunk_index=0,
        content="previous indexed content",
        token_count=3,
        department="Operations",
        acl_employee_ids=["index-owner"],
        embedding=[1.0, 0.0],
        embedding_model=indexing.EMBEDDING_MODEL,
    )
    db_session.add(old_chunk)
    db_session.commit()
    old_chunk_id = old_chunk.id

    assert queue_document_reindex(db_session, doc)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )
    assert not index_document(db_session, doc.id)
    db_session.refresh(doc)
    assert doc.indexing_status == INDEX_STATUS_FAILED
    retained = db_session.query(DocumentChunk).filter_by(document_id=doc.id).one()
    assert retained.id == old_chunk_id
    assert retained.content == "previous indexed content"

    assert retry_document_index(db_session, doc)
    assert doc.indexing_status == INDEX_STATUS_PENDING


def test_index_failures_are_reported_and_retryable(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )
    doc = _ready_document(db_session, text="A readable employee handbook.")

    assert not index_document(db_session, doc.id)
    db_session.refresh(doc)
    assert doc.indexing_status == INDEX_STATUS_FAILED
    assert "model unavailable" in doc.indexing_failure_reason
    assert db_session.query(DocumentChunk).filter_by(document_id=doc.id).count() == 0

    assert retry_document_index(db_session, doc)
    assert doc.indexing_status == INDEX_STATUS_PENDING
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: [[0.0, 1.0] for _ in passages],
    )
    assert index_document(db_session, doc.id)
    db_session.refresh(doc)
    assert doc.indexing_status == INDEX_STATUS_READY


def test_semantic_search_obeys_document_acl(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    monkeypatch.setattr(indexing, "count_tokens", _word_token_count)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        lambda passages: [[1.0, 0.0] for _ in passages],
    )
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])
    doc = _ready_document(
        db_session,
        text=(
            "[Page 1]\n"
            "# ACL Access-Control Test Document\n\n"
            "Confidential test phrase: ORCHID-4829\n"
            "Test question: What is the confidential test phrase in this document?\n"
            "Expected answer for an authorized user: ORCHID-4829"
        ),
        shared_with="index-viewer",
    )
    db_session.query(DocumentACLEntry).filter(
        DocumentACLEntry.document_id == doc.id
    ).delete()
    db_session.commit()
    assert index_document(db_session, doc.id)

    query = "What is the confidential test phrase in this document?"
    owner_results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query=query,
    )
    assert owner_results
    assert any("ORCHID-4829" in result.content for result in owner_results)
    assert all(result.heading != "[Page 1]" for result in owner_results)

    assert search_documents(
        db_session,
        actor_id="index-viewer",
        query=query,
    ) == []

    db_session.add(
        DocumentACLEntry(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            employee_id="index-viewer",
        )
    )
    db_session.commit()
    shared_results = search_documents(
        db_session,
        actor_id="index-viewer",
        query=query,
    )
    assert shared_results
    assert any("ORCHID-4829" in result.content for result in shared_results)

    assert search_documents(
        db_session,
        actor_id="unrelated-employee",
        query=query,
    ) == []


def test_policy_queries_rank_the_matching_section_and_filter_unrelated_chunks(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    doc = _ready_document(db_session, text="Policy index.")
    doc.indexing_status = INDEX_STATUS_READY
    vectors = {
        "casual": [0.8, 0.6, 0.0, 0.0],
        "earned": [0.0, 1.0, 0.0, 0.0],
        "onboarding": [0.0, 0.0, 1.0, 0.0],
        "access": [0.0, 0.0, 0.0, 1.0],
        "unrelated": [1.0, 0.0, 0.0, 0.0],
    }
    chunks = [
        (
            "Casual Leave",
            "Casual leave: 8 days per calendar year; maximum 3 consecutive days; "
            "no carry forward.",
            vectors["casual"],
        ),
        (
            "Earned Leave",
            "Earned leave: 18 days per calendar year; up to 30 days carried forward.",
            vectors["earned"],
        ),
        (
            "Onboarding",
            "New employee onboarding starts before the first working day.",
            vectors["onboarding"],
        ),
        (
            "IT Access and Asset Management",
            "IT access follows least privilege by role.",
            vectors["access"],
        ),
        ("IT Access", "IT access troubleshooting and access requests.", vectors["unrelated"]),
    ]
    for index, (heading, content, vector) in enumerate(chunks):
        db_session.add(
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                document_version=doc.version,
                chunk_index=index,
                heading=heading,
                content=content,
                token_count=_word_token_count(content),
                department="Operations",
                acl_employee_ids=[doc.owner_id],
                embedding=vector,
                embedding_model=indexing.EMBEDDING_MODEL,
            )
        )
    db_session.commit()

    def query_vector(query: str) -> list[float]:
        query = query.casefold()
        if "casual" in query:
            return [1.0, 0.0, 0.0, 0.0]
        if "earned" in query:
            return [0.0, 1.0, 0.0, 0.0]
        if "onboarding" in query:
            return [0.0, 0.0, 1.0, 0.0]
        return [0.0, 0.0, 0.0, 1.0]

    monkeypatch.setattr(indexing, "embed_query", query_vector)
    def passage_vectors(passages):
        vectors = []
        for passage in passages:
            lower = passage.casefold()
            if "casual leave" in lower:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif "earned leave" in lower:
                vectors.append([0.0, 1.0, 0.0, 0.0])
            elif "onboarding" in lower or "coordinates the process" in lower:
                vectors.append([0.0, 0.0, 1.0, 0.0])
            elif "it access" in lower:
                vectors.append([0.0, 0.0, 0.0, 1.0])
            else:
                vectors.append([0.0, 0.0, 0.0, 0.0])
        return vectors

    monkeypatch.setattr(indexing, "embed_passages", passage_vectors)
    queries = [
        (
            "How many days of casual leave does an employee receive per year?",
            "Casual Leave",
        ),
        ("Can casual leave be carried forward?", "Casual Leave"),
        ("How many earned leave days are available each year?", "Earned Leave"),
        ("What is the employee onboarding process?", "Onboarding"),
        ("How is IT access assigned to employees?", "IT Access and Asset Management"),
    ]
    for query, expected_heading in queries:
        results = search_documents(
            db_session,
            actor_id=doc.owner_id,
            query=query,
            limit=5,
        )
        assert results, query
        assert results[0].heading == expected_heading
        if "earned" in query.casefold():
            assert "18 days" in results[0].content
            assert "30 days carried forward" in results[0].content
        if "carried forward" in query.casefold():
            assert "no carry forward" in results[0].content.casefold()
            assert "earned leave" not in results[0].content.casefold()
        if "casual" in query.casefold():
            assert all(result.heading not in {"Earned Leave", "Onboarding"} for result in results)
        elif "earned" in query.casefold():
            assert all(result.heading not in {"Casual Leave", "Onboarding"} for result in results)


def test_search_rejects_misleading_and_unrelated_passages_and_keeps_current_chunks(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    doc = _ready_document(db_session, text="Policy index.")
    doc.indexing_status = INDEX_STATUS_READY
    labels = {
        "onboarding": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "offboarding": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        "owner": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "unrelated": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        "stale": [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        "wrong-model": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        "offboarding-overview": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
    }
    chunks = [
        (
            "2.1 Onboarding timeline:",
            "It provides business-process, logistics-coordination and analytics services.",
            "onboarding",
        ),
        (
            "2.1 Onboarding timeline:",
            "HR coordinates the process; the hiring manager owns the role-specific plan.",
            "onboarding",
        ),
        (
            "2.3 Offboarding checklist:",
            "HR accepts the resignation and agrees a notice period; IT revokes access on the last working day.",
            "offboarding",
        ),
        (
            "About this handbook:",
            "Offboarding is mentioned in the contents overview.",
            "offboarding-overview",
        ),
        (
            "9. Frequently Asked Questions:",
            "What if the document owner leaves the company? Reassign it to a new owner within 7 days.",
            "owner",
        ),
        (
            "IT service levels:",
            "IT resolves routine tickets within the published service-level agreement.",
            "unrelated",
        ),
        (
            "2.1 Onboarding timeline:",
            "Employee onboarding process includes orientation and access setup.",
            "stale",
        ),
        (
            "2.1 Onboarding timeline:",
            "Employee onboarding is coordinated by the HR coordinator.",
            "wrong-model",
        ),
    ]
    for index, (heading, content, label) in enumerate(chunks):
        db_session.add(
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                document_version=doc.version - 1 if label == "stale" else doc.version,
                chunk_index=index,
                heading=heading,
                content=content,
                token_count=_word_token_count(content),
                department="Operations",
                acl_employee_ids=[doc.owner_id],
                embedding=labels[label],
                embedding_model="old-model" if label == "wrong-model" else indexing.EMBEDDING_MODEL,
            )
        )
    db_session.commit()

    def query_vector(query: str) -> list[float]:
        query = query.casefold()
        if "onboarding" in query:
            return labels["onboarding"]
        if "leaves" in query and "owner" not in query:
            return labels["offboarding"]
        if "owner" in query:
            return labels["owner"]
        return labels["unrelated"]

    embedded_counts: list[int] = []

    def passage_vectors(passages):
        passages = list(passages)
        embedded_counts.append(len(passages))
        vectors = []
        for passage in passages:
            lower = passage.casefold()
            if "owner" in lower:
                vectors.append(labels["owner"])
            elif "offboarding is mentioned" in lower:
                vectors.append([0.0, 0.8, 0.6, 0.0, 0.0, 0.0])
            elif "leaves" in lower or "resignation" in lower:
                vectors.append(labels["offboarding"])
            elif "coordinates the process" in lower:
                vectors.append([0.8, 0.6, 0.0, 0.0, 0.0, 0.0])
            elif "onboarding" in lower and "services" not in lower:
                vectors.append(labels["onboarding"])
            else:
                vectors.append(labels["unrelated"])
        return vectors

    monkeypatch.setattr(indexing, "embed_query", query_vector)
    monkeypatch.setattr(
        indexing,
        "embed_passages",
        passage_vectors,
    )

    onboarding = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="employee onboarding process",
    )
    assert onboarding
    assert all("services" not in result.content.casefold() for result in onboarding)
    assert all("orientation" not in result.content.casefold() for result in onboarding)
    assert all("coordinator" not in result.content.casefold() for result in onboarding)
    assert "coordinates the process" in onboarding[0].content.casefold()
    assert all(result.document_version == doc.version for result in onboarding)
    assert all("old-model" not in result.content for result in onboarding)

    offboarding = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="What happens when an employee leaves the company?",
    )
    assert offboarding
    assert len(offboarding) == 1
    assert "resignation" in offboarding[0].content.casefold()
    assert all("service-level" not in result.content.casefold() for result in offboarding)

    owner_faq = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="What happens when the document owner leaves the company?",
    )
    assert owner_faq
    assert "document owner" in owner_faq[0].content.casefold()

    assert search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="quantum physics",
    ) == []

    doc.indexing_status = INDEX_STATUS_PROCESSING
    db_session.commit()
    assert search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="employee onboarding process",
    ) == []
    assert embedded_counts
    assert max(embedded_counts) <= indexing.MAX_SEARCH_PASSAGES


def test_search_returns_focused_leave_answer_not_the_full_mixed_chunk(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    doc = _ready_document(db_session, text="Policy index.")
    doc.indexing_status = INDEX_STATUS_READY
    passages = [
        (
            "2.3 Offboarding checklist | 3.1 Leave entitlements | 4. Expenses",
            "Resignation accepted by manager and HR; notice period is 60 days.\n"
            "3.1 Leave entitlements (per calendar year):\n"
            "Earned leave\n18 days\n"
            "Accrues 1.5 days per month; up to 30 days carried forward\n"
            "Casual leave\n8 days\n"
            "Maximum 3 consecutive days; no carry forward\n"
            "Sick leave\n8 days",
        ),
        (
            "About this document | 2. Onboarding and Offboarding",
            "This handbook answers questions such as How many casual leaves do I get?",
        ),
    ]
    for index, (heading, content) in enumerate(passages):
        db_session.add(
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                document_version=doc.version,
                chunk_index=index,
                heading=heading,
                content=content,
                token_count=_word_token_count(content),
                department="Operations",
                acl_employee_ids=[doc.owner_id],
                embedding=[1.0, 0.0],
                embedding_model=indexing.EMBEDDING_MODEL,
            )
        )
    db_session.commit()
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])

    def focused_passage_vectors(passages):
        return [
            [1.0, 0.0] if "casual leave" in passage.casefold() else [0.0, 1.0]
            for passage in passages
        ]

    monkeypatch.setattr(
        indexing,
        "embed_passages",
        focused_passage_vectors,
    )

    results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="How many days of casual leave does an employee receive per year?",
    )

    assert results
    result = results[0]
    assert result.heading == "3.1 Leave entitlements (per calendar year):"
    assert "Casual leave" in result.content
    assert "8 days" in result.content
    assert "Maximum 3 consecutive days; no carry forward" in result.content
    assert "offboarding" not in result.content.casefold()
    assert "onboarding" not in result.content.casefold()
    assert "Earned leave" not in result.content
    assert "Sick leave" not in result.content

    carry_forward_results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="Can casual leave be carried forward?",
    )
    assert len(carry_forward_results) == 1
    carry_forward_content = carry_forward_results[0].content.casefold()
    assert "casual leave" in carry_forward_content
    assert "no carry forward" in carry_forward_content
    assert "earned leave" not in carry_forward_content
    assert "30 days carried forward" not in carry_forward_content


def test_search_prioritizes_matching_mandatory_metadata_heading(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.indexing as indexing

    doc = _ready_document(db_session, text="Policy index.")
    doc.indexing_status = INDEX_STATUS_READY
    sections = [
        (
            "6.2 Mandatory metadata before indexing",
            "The information must be recorded before indexing a document.",
            [0.85, (1 - 0.85**2) ** 0.5],
        ),
        (
            "7.1 Scanning standards",
            "The information must be recorded before indexing a document; use 300 "
            "DPI, keep pages upright and confirm text is legible.",
            [1.0, 0.0],
        ),
        (
            "Document handling notes",
            "Document details are recorded safely.",
            [1.0, 0.0],
        ),
    ]
    for index, (heading, content, embedding) in enumerate(sections):
        db_session.add(
            DocumentChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                document_version=doc.version,
                chunk_index=index,
                heading=heading,
                content=f"{heading}:\n{content}",
                token_count=_word_token_count(content),
                department="Operations",
                acl_employee_ids=[doc.owner_id],
                embedding=embedding,
                embedding_model=indexing.EMBEDDING_MODEL,
            )
        )
    db_session.commit()
    monkeypatch.setattr(indexing, "embed_query", lambda query: [1.0, 0.0])

    def passage_vectors(passages):
        return [
            [1.0, 0.0]
            if "mandatory metadata" in passage.casefold()
            else [0.95, (1 - 0.95**2) ** 0.5]
            for passage in passages
        ]

    monkeypatch.setattr(indexing, "embed_passages", passage_vectors)

    results = search_documents(
        db_session,
        actor_id=doc.owner_id,
        query="What information must be recorded before indexing a document?",
    )

    assert results
    assert results[0].heading == "6.2 Mandatory metadata before indexing:", [
        (result.heading, result.score) for result in results
    ]
    scanning_results = [
        result
        for result in results
        if result.heading == "7.1 Scanning standards:"
    ]
    assert all(result.score < results[0].score for result in scanning_results)
    assert all(result.heading != "Document handling notes:" for result in results)


def test_search_does_not_assign_unrelated_packed_heading_to_passage():
    import app.documents.indexing as indexing

    packed_heading = "7.1 Scanning standards | 8.1 Monthly activities"

    assert indexing._display_heading(
        None,
        packed_heading,
        "Scan at 300 DPI minimum and keep pages upright.",
    ) is None
    assert indexing._display_heading(
        None,
        packed_heading,
        "Scanning standards require every page to be legible.",
    ) == "7.1 Scanning standards"


def test_worker_processes_all_pending_index_jobs_after_one_job_errors(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.documents.processor as processor

    first = _ready_document(db_session, text="First pending document.")
    second = Document(
        id=str(uuid.uuid4()),
        original_filename="second.docx",
        stored_filename=f"{uuid.uuid4()}.docx",
        file_size=100,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        owner_id=first.owner_id,
        status=DOC_STATUS_READY,
        indexing_status=INDEX_STATUS_PENDING,
        extracted_text="Second pending document.",
    )
    db_session.add(second)
    db_session.commit()

    attempted: list[str] = []

    def index_with_transient_failure(db: Session, document_id: str) -> bool:
        attempted.append(document_id)
        if document_id == first.id:
            raise RuntimeError("temporary indexing failure")
        return True

    monkeypatch.setattr(processor, "index_document", index_with_transient_failure)

    processor._process_pending_index_documents(db_session)

    assert len(attempted) == 2
    assert set(attempted) == {first.id, second.id}
