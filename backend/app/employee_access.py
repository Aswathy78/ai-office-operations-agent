"""Shared employee visibility rules for manager-scoped operations."""

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import Employee


def manager_visible_employee_ids(
    db: Session,
    principal: AuthenticatedPrincipal,
) -> set[str]:
    return manager_visible_employee_ids_for(db, principal.user_id)


def manager_visible_employee_ids_for(db: Session, manager_id: str) -> set[str]:
    """Return self and active same-department reports under existing line rules.

    The historical unassigned-same-team fallback remains, but only when both
    records have a known matching department. No department is inferred from team.
    """
    manager = db.get(Employee, manager_id)
    visible = {manager_id}
    if manager is None or not manager.is_active:
        return visible
    department = _known_value(manager.department)
    if department is None:
        return visible

    clauses = [Employee.manager_id == manager.id]
    team = _known_value(manager.team)
    if team is not None:
        clauses.append(
            and_(
                Employee.manager_id.is_(None),
                func.lower(func.trim(Employee.team)) == team,
                Employee.id != manager.id,
            )
        )
    department_filter = func.lower(func.trim(Employee.department)) == department
    visible.update(
        employee_id
        for (employee_id,) in db.query(Employee.id)
        .filter(
            Employee.is_active.is_(True),
            department_filter,
            or_(*clauses),
        )
        .all()
    )
    return visible


def department_visible_employee_ids(
    db: Session,
    principal: AuthenticatedPrincipal,
) -> set[str]:
    """Return records visible by department, further limiting managers by line."""
    if principal.role in {"hr_operations", "admin"}:
        return {
            employee_id
            for (employee_id,) in db.query(Employee.id)
            .filter(Employee.is_active.is_(True))
            .all()
        }

    visible = {principal.user_id}
    actor = db.get(Employee, principal.user_id)
    department = _known_value(actor.department) if actor is not None and actor.is_active else None
    if department is None:
        return visible

    in_department = {
        employee_id
        for (employee_id,) in db.query(Employee.id)
        .filter(
            Employee.is_active.is_(True),
            func.lower(func.trim(Employee.department)) == department,
        )
        .all()
    }
    if principal.role == "manager":
        return in_department & manager_visible_employee_ids_for(db, principal.user_id)
    if principal.role == "employee":
        return in_department | visible
    return visible


def _known_value(value: str | None) -> str | None:
    return value.strip().casefold() if value and value.strip() else None
