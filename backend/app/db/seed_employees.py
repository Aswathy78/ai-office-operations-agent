from __future__ import annotations

import os
from pathlib import Path
import uuid

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.meeting.models import Employee

DEVELOPMENT_EMPLOYEES = [
    {"name": "System Administrator", "email": "admin@northstar.example", "team": "Operations"},
    {"name": "Maya Thompson", "email": "manager@example.com", "team": "Engineering"},
    {"name": "Ava Patel", "email": "ava.patel@northstar.example", "team": "Engineering"},
    {"name": "Marcus Chen", "email": "marcus.chen@northstar.example", "team": "Engineering"},
    {"name": "Isabella Lopez", "email": "isabella.lopez@northstar.example", "team": "Finance"},
    {"name": "Daniel Brooks", "email": "daniel.brooks@northstar.example", "team": "Engineering"},
    {"name": "Priya Sharma", "email": "priyasharma.example@gmail.com","team": "Engineering"},
    {"name": "Noah Martin", "email": "noah.martin@northstar.example", "team": "Finance"},
    {"name": "Chloe Nguyen", "email": "chloe.nguyen@northstar.example", "team": "Operations"},
    {"name": "Ethan Walker", "email": "ethan.walker@northstar.example", "team": "Operations"},
    {"name": "Rahul", "email": "rahulexample.test@gmail.com", "team": "Engineering"},
]

# Line manager (by e-mail) for each team; receives reminder escalations (Story 4.4).
# Maya Thompson matches the demo "manager" login.
TEAM_MANAGERS = {
    "Engineering": "manager@example.com",
    "Finance": "isabella.lopez@northstar.example",
    "Operations": "chloe.nguyen@northstar.example",
}


def seed_development_employees(db: Session) -> list[Employee]:
    """Create a small fictional employee directory if it does not already exist."""
    created: list[Employee] = []
    updated = False

    for employee_data in DEVELOPMENT_EMPLOYEES:
        employee = db.query(Employee).filter(Employee.email == employee_data["email"]).first()
        if employee is not None:
            # Back-fill teams for employees seeded before teams existed.
            if employee.team is None:
                employee.team = employee_data["team"]
                updated = True
            continue

        employee = Employee(
            id=str(uuid.uuid4()),
            name=employee_data["name"],
            email=employee_data["email"],
            team=employee_data["team"],
            is_active=True,
        )
        db.add(employee)
        created.append(employee)

    if created:
        db.flush()
    updated = _assign_team_managers(db) or updated

    if created or updated:
        db.commit()
        for employee in created:
            db.refresh(employee)

    return created


def _assign_team_managers(db: Session) -> bool:
    """Back-fill missing line managers for seeded employees; never overrides a set manager."""
    seeded = {data["email"] for data in DEVELOPMENT_EMPLOYEES}
    managers = {
        team: db.query(Employee).filter(Employee.email == email).first()
        for team, email in TEAM_MANAGERS.items()
    }
    updated = False
    for employee in db.query(Employee).filter(Employee.email.in_(seeded)).all():
        manager = managers.get(employee.team or "")
        if manager is None or manager.id == employee.id or employee.manager_id is not None:
            continue
        employee.manager_id = manager.id
        updated = True
    return updated


def require_development_environment() -> None:
    if os.environ.get("APP_ENV", "").strip().casefold() != "development":
        raise SystemExit(
            "Refusing to seed employees unless APP_ENV=development is set."
        )


def main() -> None:
    load_dotenv(
        dotenv_path=Path(__file__).resolve().parents[2] / ".env",
        override=False,
    )
    require_development_environment()

    from app.db.database import DATABASE_PATH, SessionLocal

    with SessionLocal() as db:
        created = seed_development_employees(db)

    print(
        f"Development employee seed complete: created {len(created)} employee(s) "
        f"in {DATABASE_PATH}."
    )


if __name__ == "__main__":
    main()
