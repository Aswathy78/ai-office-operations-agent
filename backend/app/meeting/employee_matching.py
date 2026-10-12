from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from sqlalchemy.orm import Session

from app.meeting.models import Employee

MATCH_CONFIDENCE_THRESHOLD = 0.88
AMBIGUITY_MARGIN = 0.05


@dataclass(frozen=True)
class EmployeeMatchResult:
    """Outcome of comparing an extracted owner name against active employees."""

    status: Literal["unique_match", "ambiguous_match", "no_match", "missing_owner"]
    owner_name: str | None = None
    employee_id: str | None = None
    employee_name: str | None = None
    confidence: float | None = None
    candidate_employee_ids: list[str] = field(default_factory=list)
    candidate_names: list[str] = field(default_factory=list)


def _normalize_name(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.strip().split()).casefold()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def match_employee_name(
    db: Session,
    owner_name: str | None,
) -> EmployeeMatchResult:
    """Match an extracted owner name against active employees.

    Confidence rule:
    - EXACT or near-exact matches above MATCH_CONFIDENCE_THRESHOLD are candidates.
    - Exactly one confident candidate is a unique match.
    - Multiple confident candidates are ambiguous and require organizer approval.
    """
    if owner_name is None or not owner_name.strip():
        return EmployeeMatchResult(status="missing_owner", owner_name=owner_name)

    normalized_owner = _normalize_name(owner_name)
    employees = (
        db.query(Employee)
        .filter(Employee.is_active.is_(True))
        .order_by(Employee.name.asc())
        .all()
    )

    if not employees:
        return EmployeeMatchResult(
            status="no_match",
            owner_name=owner_name,
            confidence=0.0,
        )

    ranked = []
    for employee in employees:
        score = _similarity(normalized_owner, _normalize_name(employee.name))
        ranked.append({"employee": employee, "score": score})

    ranked.sort(key=lambda item: item["score"], reverse=True)
    top = ranked[0]
    top_score = top["score"]
    confident_matches = [
        item for item in ranked if item["score"] >= MATCH_CONFIDENCE_THRESHOLD
    ]
    if len(confident_matches) > 1:
        return EmployeeMatchResult(
            status="ambiguous_match",
            owner_name=owner_name,
            confidence=top_score,
            candidate_employee_ids=[
                item["employee"].id for item in confident_matches
            ],
            candidate_names=[
                item["employee"].name for item in confident_matches
            ],
        )

    if confident_matches:
        employee = confident_matches[0]["employee"]
        return EmployeeMatchResult(
            status="unique_match",
            owner_name=owner_name,
            employee_id=employee.id,
            employee_name=employee.name,
            confidence=top_score,
        )

    return EmployeeMatchResult(
        status="no_match",
        owner_name=owner_name,
        confidence=top_score,
        candidate_employee_ids=[item["employee"].id for item in ranked[:3]],
        candidate_names=[item["employee"].name for item in ranked[:3]],
    )
