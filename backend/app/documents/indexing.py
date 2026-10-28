"""Asynchronous document chunking, local vector persistence, and search."""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.documents.chunking import DocumentChunkText, chunk_document_text
from app.documents.embeddings import (
    EMBEDDING_MODEL,
    count_tokens,
    embed_passages,
    embed_query,
)
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
from app.documents.service import _document_access_filter
from app.meeting.models import Employee

logger = logging.getLogger(__name__)
MAX_FAILURE_REASON_LENGTH = 400
_SEARCH_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
        "for", "from", "get", "how", "i", "in", "is", "it", "many", "of",
        "on", "or", "per", "please", "the", "their", "they", "to", "we",
        "what", "when", "where", "which", "who", "why", "with", "you",
        "day", "days", "does", "employee", "employees", "company", "calendar",
        "policy", "receive", "year", "happen", "happens", "available", "each",
    }
)
MAX_SEARCH_CANDIDATE_CHUNKS = 20
MAX_PASSAGES_PER_CHUNK = 400
MAX_SEARCH_PASSAGES = 32
MAX_RESULT_SCORE_GAP = 0.08
MIN_PASSAGE_SIMILARITY = 0.81
MIN_FALLBACK_PASSAGE_SIMILARITY = 0.72
MIN_STRONG_BODY_TERM_COVERAGE = 0.40
MIN_FACTUAL_TERM_COVERAGE = 0.65
MIN_BODY_TERM_COVERAGE = 0.20
MIN_RELEVANT_BODY_TERM_COVERAGE = 0.35
MIN_HEADING_TERM_COVERAGE = 0.15
_MEASURE_QUERY = re.compile(
    r"\b(?:days?|weeks?|months?|years?|hours?|minutes?|percent(?:age)?|rupees?|inr)\b",
    re.I,
)
_MEASURE_VALUE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:days?|weeks?|months?|years?|hours?|minutes?|percent(?:age)?|rupees?|inr)\b",
    re.I,
)
_CARRY_FORWARD_QUERY = re.compile(
    r"\b(?:carry|carried)\s+forward\b",
    re.I,
)
_CARRY_FORWARD_POLICY = re.compile(
    r"\b(?:no|not|cannot|can't|may\s+not)\s+carry\s+forward\b"
    r"|\b(?:carry|carried)\s+forward\b",
    re.I,
)
_LABELED_ANSWER = re.compile(r"^\s*(?:(?:expected|correct)\s+)?answer\b", re.I)
_BULLET_SEPARATOR = re.compile(r"(?:^|\s)•\s+")
_TABLE_ROW = re.compile(r".+\s\|\s.+")
_CONSTRAINT_CONTEXT = re.compile(
    r"\b(?:maximum|minimum|consecutive|carry[\s-]+forward|accru\w*|per calendar year)\b",
    re.I,
)
_OTHER_LEAVE_TYPE = re.compile(
    r"^\s*[-*]?\s*(?:earned|sick|maternity|paternity|bereavement|compensatory)\s+leave\b",
    re.I,
)
_LEAVE_TYPE_MENTION = re.compile(
    r"\b(casual|earned|sick|maternity|paternity|bereavement|compensatory)\s+leave\b",
    re.I,
)
_SECTION_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+|\d+(?:\.\d+)+(?:[.)])?\s+\S|\d+[.)]\s+\S|\[(?:Page|Slide|Sheet)\b)",
    re.I,
)
_STRUCTURAL_HEADING = re.compile(r"^\s*\[(?:Page|Slide|Sheet)\b", re.I)
_DOCUMENT_OWNER_PHRASE = re.compile(r"\bdocument\s+owner\b", re.I)
_DOCUMENT_OWNER_REASSIGNMENT = re.compile(
    r"\b(?:owner|ownership)\b[\s\S]{0,160}\breassign\w*"
    r"|\breassign\w*[\s\S]{0,160}\b(?:owner|ownership)\b",
    re.I,
)
_DEPARTED_DOCUMENT_REASSIGNMENT = re.compile(
    r"\bdeparted employees?' documents?\b[\s\S]{0,100}\breassign\w*",
    re.I,
)
_SEARCH_TERM_SYNONYMS = {
    "leaves": frozenset(
        {"departed", "departure", "offboarding", "resign", "resignation"}
    )
}


@dataclass(frozen=True)
class SemanticSearchResult:
    document_id: str
    filename: str
    heading: str | None
    content: str
    score: float
    document_version: int


def _search_terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.casefold()))
    return {
        term[:-3] + "y"
        if len(term) > 5 and term.endswith("ies")
        else term[:-1]
        if (
            len(term) > 4
            and term.endswith("s")
            and not term.endswith(("leaves", "ss"))
        )
        else term
        for term in terms
        if len(term) > 2 and term not in _SEARCH_STOP_WORDS
    }


def _contains_search_term(term: str, content_terms: set[str]) -> bool:
    return term in content_terms or bool(
        _SEARCH_TERM_SYNONYMS.get(term, frozenset()).intersection(content_terms)
    )


def _search_passages(content: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_units: list[str] = []
    page_title_pending = False

    def flush() -> None:
        if current_units:
            sections.append((current_heading, current_units.copy()))
            current_units.clear()

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if _SECTION_HEADING.match(line) or (
            line.endswith(":") and len(line.split()) <= 18
        ):
            flush()
            current_heading = line.lstrip("#").strip()
            page_title_pending = bool(_STRUCTURAL_HEADING.match(line))
            continue

        if page_title_pending:
            page_title_pending = False
            title_words = re.findall(r"[A-Za-z][A-Za-z'-]*", line)
            if (
                1 < len(title_words) <= 12
                and not line.endswith((".", "!", "?", ":"))
                and all(word.isupper() or word[0].isupper() for word in title_words)
            ):
                current_heading = f"{line} — {current_heading}"
                continue

        bullet_items = _BULLET_SEPARATOR.split(line)
        if len(bullet_items) > 1:
            prefix = bullet_items[0].strip()
            if prefix:
                current_units.extend(
                    unit.strip()
                    for unit in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", prefix)
                    if unit.strip()
                )
            list_content = "\n".join(
                f"• {item.strip()}" for item in bullet_items[1:] if item.strip()
            )
            if list_content:
                if current_units and current_units[-1].startswith("• "):
                    current_units[-1] += f"\n{list_content}"
                else:
                    current_units.append(list_content)
            continue
        if _TABLE_ROW.fullmatch(line):
            if current_units and _TABLE_ROW.fullmatch(current_units[-1].splitlines()[-1]):
                current_units[-1] += f"\n{line}"
            else:
                current_units.append(line)
            continue

        bullets = re.split(r"\s+-\s+(?=[A-Z])", line)
        for bullet in bullets:
            current_units.extend(
                unit.strip()
                for unit in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", bullet)
                if unit.strip()
            )
    flush()

    passages: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()
    for heading, units in sections:
        grouped_units: list[str] = []
        index = 0
        while index < len(units):
            unit = units[index]
            if _LEAVE_TYPE_MENTION.match(unit):
                group = [unit]
                index += 1
                while index < len(units) and not _LEAVE_TYPE_MENTION.match(units[index]):
                    group.append(units[index])
                    index += 1
                grouped_units.append("\n".join(group))
            else:
                grouped_units.append(unit)
                index += 1

        for start in range(len(grouped_units)):
            if (
                start > 0
                and grouped_units[start - 1].rstrip().endswith("?")
                and _LABELED_ANSWER.match(grouped_units[start])
            ):
                continue
            if (
                grouped_units[start].rstrip().endswith("?")
                and start + 1 < len(grouped_units)
                and _LABELED_ANSWER.match(grouped_units[start + 1])
            ):
                text = "\n".join(grouped_units[start : start + 2])
                key = (heading, text)
                if key not in seen:
                    seen.add(key)
                    passages.append(key)
                continue

            if _LEAVE_TYPE_MENTION.match(grouped_units[start]):
                selected = [grouped_units[start]]
            else:
                selected = []
                for unit in grouped_units[start : start + 3]:
                    if _OTHER_LEAVE_TYPE.match(unit) and selected:
                        break
                    selected.append(unit)
            text = "\n".join(selected)
            key = (heading, text)
            if key not in seen:
                seen.add(key)
                passages.append(key)
    return passages[:MAX_PASSAGES_PER_CHUNK]


def _display_heading(
    section_heading: str | None,
    indexed_heading: str | None,
    content: str,
) -> str | None:
    if section_heading and not _STRUCTURAL_HEADING.match(section_heading):
        return section_heading.lstrip("#").strip()
    if indexed_heading:
        content_terms = _search_terms(content)
        candidates = [
            part.strip()
            for part in indexed_heading.split("|")
            if part.strip()
        ]
        if candidates:
            best_candidate = max(
                candidates,
                key=lambda part: (
                    len(_search_terms(part).intersection(content_terms)),
                    -len(part),
                ),
            )
            if _search_terms(best_candidate).intersection(content_terms):
                if section_heading:
                    return f"{best_candidate} — {section_heading.strip()}"
                return best_candidate
    return section_heading.strip() if section_heading else None


def _failure_reason(exc: Exception) -> str:
    reason = str(exc).strip() or type(exc).__name__
    return reason[:MAX_FAILURE_REASON_LENGTH]


def index_document(db: Session, document_id: str) -> bool:
    """Claim and index one extracted document; duplicate workers are harmless."""
    claim = db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status == DOC_STATUS_READY,
            Document.indexing_status == INDEX_STATUS_PENDING,
        )
        .values(
            indexing_status=INDEX_STATUS_PROCESSING,
            indexing_failure_reason=None,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if claim.rowcount != 1:
        db.rollback()
        return False
    db.commit()

    doc = db.get(Document, document_id)
    if doc is None:
        return False
    version = doc.version

    try:
        if not doc.extracted_text or not doc.extracted_text.strip():
            raise ValueError("The extracted document has no text to index.")
        chunks = chunk_document_text(doc.extracted_text, count_tokens)
        if not chunks:
            raise ValueError("The extracted document produced no searchable text.")
        vectors = embed_passages(chunk.content for chunk in chunks)
        if len(vectors) != len(chunks):
            raise RuntimeError("The embedding model returned an incomplete result.")
        if any(not vector or not all(math.isfinite(value) for value in vector) for vector in vectors):
            raise RuntimeError("The embedding model returned an invalid vector.")

        owner = db.get(Employee, doc.owner_id)
        if owner is None:
            raise RuntimeError("The document owner record is unavailable.")
        acl_ids = [doc.owner_id]
        acl_ids.extend(
            row[0]
            for row in db.query(DocumentACLEntry.employee_id)
            .filter(DocumentACLEntry.document_id == doc.id)
            .order_by(DocumentACLEntry.employee_id)
            .all()
        )
        acl_ids = sorted(set(acl_ids))

        current = (
            db.query(Document)
            .populate_existing()
            .filter(Document.id == document_id)
            .one_or_none()
        )
        if (
            current is None
            or current.version != version
            or current.status != DOC_STATUS_READY
            or current.indexing_status != INDEX_STATUS_PROCESSING
        ):
            db.rollback()
            return False

        db.query(DocumentChunk).filter(
            DocumentChunk.document_id == document_id
        ).delete(synchronize_session=False)
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                DocumentChunk(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    document_version=version,
                    chunk_index=index,
                    heading=chunk.heading,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    department=owner.team,
                    acl_employee_ids=acl_ids,
                    embedding=vector,
                    embedding_model=EMBEDDING_MODEL,
                    created_at=now,
                )
                for index, (chunk, vector) in enumerate(zip(chunks, vectors))
            ]
        )
        current.indexing_status = INDEX_STATUS_READY
        current.indexing_failure_reason = None
        current.updated_at = now
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.exception("Document indexing failed for %s", document_id)
        reason = _failure_reason(exc)
        db.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.version == version,
                Document.status == DOC_STATUS_READY,
                Document.indexing_status == INDEX_STATUS_PROCESSING,
            )
            .values(
                indexing_status=INDEX_STATUS_FAILED,
                indexing_failure_reason=reason,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        return False


def search_documents(
    db: Session,
    *,
    actor_id: str,
    query: str,
    limit: int = 10,
) -> list[SemanticSearchResult]:
    """Rank ready chunks only from documents the actor may access."""
    normalized_query = query.strip()
    if not normalized_query:
        return []

    has_accessible_documents = (
        db.query(Document.id)
        .filter(_document_access_filter(actor_id))
        .first()
    )
    if has_accessible_documents is None:
        return []

    query_vector = embed_query(normalized_query)
    rows = (
        db.query(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .filter(
            _document_access_filter(actor_id),
            DocumentChunk.document_version == Document.version,
            DocumentChunk.embedding_model == EMBEDDING_MODEL,
            Document.indexing_status == INDEX_STATUS_READY,
        )
        .all()
    )

    query_terms = _search_terms(normalized_query)
    if not query_terms:
        return []

    document_frequency = {
        term: sum(
            _contains_search_term(term, _search_terms(chunk.content))
            for chunk, _ in rows
        )
        for term in query_terms
    }
    term_weights = {
        term: math.log((len(rows) + 1) / (frequency + 1)) + 1
        for term, frequency in document_frequency.items()
    }
    total_term_weight = sum(term_weights.values()) or 1.0

    def cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)

    chunk_scores = [
        (cosine(query_vector, chunk.embedding), chunk, doc)
        for chunk, doc in rows
    ]
    chunk_scores.sort(key=lambda item: item[0], reverse=True)

    candidates: list[tuple[float, DocumentChunk, Document, str | None, str]] = []
    seen: set[tuple[str, str | None, str]] = set()
    for chunk_score, chunk, doc in chunk_scores[:MAX_SEARCH_CANDIDATE_CHUNKS]:
        for heading, content in _search_passages(chunk.content):
            key = (doc.id, heading, content)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((chunk_score, chunk, doc, heading, content))
    if not candidates:
        return []

    def candidate_priority(
        candidate: tuple[float, DocumentChunk, Document, str | None, str],
    ) -> tuple[float, float, int]:
        chunk_score, _, _, heading, content = candidate
        content_terms = _search_terms(content)
        lexical_coverage = sum(
            weight
            for term, weight in term_weights.items()
            if _contains_search_term(term, content_terms)
        ) / total_term_weight
        heading_terms = _search_terms(heading or "")
        heading_coverage = sum(
            weight
            for term, weight in term_weights.items()
            if _contains_search_term(term, heading_terms)
        ) / total_term_weight
        combined = (
            0.50 * max(chunk_score, 0.0)
            + 0.35 * lexical_coverage
            + 0.15 * heading_coverage
        )
        return combined, lexical_coverage, -len(content)

    candidates = sorted(candidates, key=candidate_priority, reverse=True)[
        :MAX_SEARCH_PASSAGES
    ]
    passage_vectors = embed_passages(content for _, _, _, _, content in candidates)
    if len(passage_vectors) != len(candidates):
        raise RuntimeError("The embedding model returned incomplete search passages.")

    factual_query = bool(_MEASURE_QUERY.search(normalized_query))
    query_leave_types = {
        match.casefold()
        for match in _LEAVE_TYPE_MENTION.findall(normalized_query)
    }
    carry_forward_query = bool(_CARRY_FORWARD_QUERY.search(normalized_query))

    ranked: list[SemanticSearchResult] = []
    for candidate, passage_vector in zip(candidates, passage_vectors):
        chunk_score, chunk, doc, heading, content = candidate
        passage_score = cosine(query_vector, passage_vector)
        content_terms = _search_terms(content)
        passage_leave_types = {
            match.casefold()
            for match in _LEAVE_TYPE_MENTION.findall(content)
        }
        lexical_coverage = sum(
            weight
            for term, weight in term_weights.items()
            if _contains_search_term(term, content_terms)
        ) / total_term_weight
        heading_terms = _search_terms(heading or "")
        heading_coverage = sum(
            weight
            for term, weight in term_weights.items()
            if _contains_search_term(term, heading_terms)
        ) / total_term_weight

        if factual_query:
            if (
                not _MEASURE_VALUE.search(content)
                or lexical_coverage < MIN_FACTUAL_TERM_COVERAGE
            ):
                continue
            if query_leave_types and not any(
                (leave_match := re.search(
                    rf"\b{re.escape(leave_type)}\s+leave\b",
                    content,
                    re.I,
                ))
                and (
                    measure_match := _MEASURE_VALUE.search(
                        content,
                        leave_match.end(),
                    )
                )
                and measure_match.start() - leave_match.end() <= 80
                for leave_type in query_leave_types
            ):
                continue
        if query_leave_types:
            if not query_leave_types.issubset(passage_leave_types):
                continue
            if passage_leave_types - query_leave_types:
                continue
            if carry_forward_query:
                leave_match = next(
                    (
                        re.search(
                            rf"\b{re.escape(leave_type)}\s+leave\b",
                            content,
                            re.I,
                        )
                        for leave_type in query_leave_types
                    ),
                    None,
                )
                if leave_match is None:
                    continue
                policy_match = _CARRY_FORWARD_POLICY.search(
                    content,
                    leave_match.end(),
                )
                if policy_match is None or policy_match.start() - leave_match.end() > 160:
                    continue
        elif lexical_coverage < MIN_BODY_TERM_COVERAGE:
            continue

        has_strong_body_match = (
            lexical_coverage >= MIN_RELEVANT_BODY_TERM_COVERAGE
        )
        has_heading_and_body_match = (
            heading_coverage >= MIN_HEADING_TERM_COVERAGE
            and lexical_coverage >= MIN_BODY_TERM_COVERAGE
        )
        if not (has_strong_body_match or has_heading_and_body_match):
            continue
        if passage_score < MIN_PASSAGE_SIMILARITY and not (
            passage_score >= MIN_FALLBACK_PASSAGE_SIMILARITY
            and has_strong_body_match
        ):
            continue

        if (
            (
                _DOCUMENT_OWNER_PHRASE.search(content)
                or _DOCUMENT_OWNER_REASSIGNMENT.search(content)
                or _DEPARTED_DOCUMENT_REASSIGNMENT.search(content)
            )
            and "owner" not in query_terms
            and "document" not in query_terms
        ):
            continue

        combined_score = (
            0.75 * passage_score
            + 0.20 * lexical_coverage
            + 0.05 * heading_coverage
            + (
                0.02 * min(len(_MEASURE_VALUE.findall(content)), 2)
                if factual_query
                else 0.0
            )
            + (
                0.03 * min(len(_CONSTRAINT_CONTEXT.findall(content)), 2)
                if _CONSTRAINT_CONTEXT.search(normalized_query)
                else 0.0
            )
        )
        ranked.append(
            SemanticSearchResult(
                document_id=doc.id,
                filename=doc.original_filename,
                heading=_display_heading(heading, chunk.heading, content),
                content=content,
                score=combined_score,
                document_version=chunk.document_version,
            )
        )

    ranked.sort(key=lambda result: result.score, reverse=True)
    if ranked:
        minimum_result_score = ranked[0].score - MAX_RESULT_SCORE_GAP
        ranked = [
            result for result in ranked
            if result.score >= minimum_result_score
        ]
    unique_results: list[SemanticSearchResult] = []
    seen_passages: set[tuple[str, str]] = set()
    seen_content_terms: list[tuple[str, set[str]]] = []
    for result in ranked:
        passage_key = (result.document_id, result.content)
        if passage_key in seen_passages:
            continue
        content_terms = _search_terms(result.content)
        if len(content_terms) >= 4:
            is_near_duplicate = False
            for index, (previous_document_id, previous_terms) in enumerate(
                seen_content_terms
            ):
                if previous_document_id != result.document_id or len(previous_terms) < 4:
                    continue
                union_size = len(content_terms.union(previous_terms))
                similarity = (
                    len(content_terms.intersection(previous_terms)) / union_size
                    if union_size
                    else 0.0
                )
                if similarity >= 0.80:
                    is_near_duplicate = True
                    previous_result = unique_results[index]
                    if (
                        result.score >= previous_result.score - 0.02
                        and len(result.content) > len(previous_result.content)
                    ):
                        unique_results[index] = result
                        seen_passages.add(passage_key)
                        seen_content_terms[index] = (
                            result.document_id,
                            content_terms,
                        )
                    break
            if is_near_duplicate:
                continue
        seen_passages.add(passage_key)
        seen_content_terms.append((result.document_id, content_terms))
        unique_results.append(result)
        if len(unique_results) >= limit:
            break
    return unique_results
