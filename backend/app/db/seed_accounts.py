"""Seed default authentication accounts for development and testing."""

from __future__ import annotations

from pathlib import Path
import uuid
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)
if "APP_ENV" not in os.environ:
    os.environ["APP_ENV"] = "development"

from app.auth_service import PASSWORD_HASHER, normalize_email
from app.db.database import SessionLocal, create_tables
from app.db.seed_employees import seed_development_employees
from app.meeting.models import AuthAccount, Employee, SystemAdministrator

DEFAULT_ACCOUNTS = [
    {
        "name": "System Administrator",
        "email": "admin@northstar.example",
        "password": "AdminSecurePass123!",
        "role": "admin",
        "team": "Operations",
    },
    {
        "name": "Maya Thompson",
        "email": "manager@example.com",
        "password": "ManagerSecurePass123!",
        "role": "manager",
        "team": "Engineering",
    },
    {
        "name": "Ava Patel",
        "email": "ava.patel@northstar.example",
        "password": "EmployeeSecurePass123!",
        "role": "employee",
        "team": "Engineering",
    },
    {
        "name": "Rahul",
        "email": "rahulexample.test@gmail.com",
        "password": "EmployeeSecurePass123!",
        "role": "manager",
        "team": "Engineering",
    },
]


def seed_accounts() -> None:
    create_tables()

    with SessionLocal() as db:
        # Ensure base employees exist
        seed_development_employees(db)

        for item in DEFAULT_ACCOUNTS:
            norm_email = normalize_email(item["email"])
            employee = (
                db.query(Employee)
                .filter(Employee.email == item["email"])
                .first()
            )

            if not employee:
                employee = Employee(
                    id=str(uuid.uuid4()),
                    name=item["name"],
                    email=item["email"],
                    team=item["team"],
                    is_active=True,
                )
                db.add(employee)
                db.flush()

            account = (
                db.query(AuthAccount)
                .filter(AuthAccount.email_normalized == norm_email)
                .first()
            )

            password_hash = PASSWORD_HASHER.hash(item["password"])
            if account:
                account.password_hash = password_hash
                account.role = item["role"]
                account.is_enabled = True
                account.employee_id = employee.id
                print(f"[UPDATED] {item['role'].capitalize()}: {item['email']} (Password: {item['password']})")
            else:
                account = AuthAccount(
                    id=str(uuid.uuid4()),
                    employee_id=employee.id,
                    email_normalized=norm_email,
                    password_hash=password_hash,
                    role=item["role"],
                    is_enabled=True,
                )
                db.add(account)
                print(f"[CREATED] {item['role'].capitalize()}: {item['email']} (Password: {item['password']})")

            # Dedicated SystemAdministrator table persistence for admin accounts
            if item["role"] == "admin":
                admin_record = (
                    db.query(SystemAdministrator)
                    .filter(SystemAdministrator.email_normalized == norm_email)
                    .first()
                )
                now = datetime.now(timezone.utc)
                if admin_record:
                    admin_record.password_hash = password_hash
                    admin_record.full_name = item["name"]
                    admin_record.is_active = True
                    admin_record.employee_id = employee.id
                    admin_record.updated_at = now
                    print(f"[UPDATED SYSTEM_ADMINISTRATOR TABLE] {item['email']}")
                else:
                    admin_record = SystemAdministrator(
                        id=str(uuid.uuid4()),
                        employee_id=employee.id,
                        email_normalized=norm_email,
                        password_hash=password_hash,
                        full_name=item["name"],
                        role="admin",
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(admin_record)
                    print(f"[CREATED SYSTEM_ADMINISTRATOR TABLE] {item['email']}")

        db.commit()


def main() -> None:
    seed_accounts()
    print("Default accounts successfully seeded in database.")


if __name__ == "__main__":
    main()
