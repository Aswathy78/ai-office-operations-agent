from __future__ import annotations

import os
from pathlib import Path
import uuid

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.meeting.models import Employee

DEVELOPMENT_EMPLOYEES = [
    {"name": "Ava Patel", "email": "ava.patel@northstar.example"},
    {"name": "Marcus Chen", "email": "marcus.chen@northstar.example"},
    {"name": "Isabella Lopez", "email": "isabella.lopez@northstar.example"},
    {"name": "Daniel Brooks", "email": "daniel.brooks@northstar.example"},
    {"name": "Priya Sharma", "email": "priya.sharma@northstar.example"},
    {"name": "Noah Martin", "email": "noah.martin@northstar.example"},
    {"name": "Chloe Nguyen", "email": "chloe.nguyen@northstar.example"},
    {"name": "Ethan Walker", "email": "ethan.walker@northstar.example"},
    {"name": "Rahul", "email": "rahulexample.test@gmail.com"},
]


def seed_development_employees(db: Session) -> list[Employee]:
    """Create a small fictional employee directory if it does not already exist."""
    created: list[Employee] = []

    for employee_data in DEVELOPMENT_EMPLOYEES:
        employee = db.query(Employee).filter(Employee.email == employee_data["email"]).first()
        if employee is not None:
            continue

        employee = Employee(
            id=str(uuid.uuid4()),
            name=employee_data["name"],
            email=employee_data["email"],
            is_active=True,
        )
        db.add(employee)
        created.append(employee)

    if created:
        db.commit()
        for employee in created:
            db.refresh(employee)

    return created


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
