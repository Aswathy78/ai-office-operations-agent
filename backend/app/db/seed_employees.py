from __future__ import annotations

import uuid

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
