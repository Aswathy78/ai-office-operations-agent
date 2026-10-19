"""Operator-only interactive provisioning for existing employee accounts."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys

from dotenv import load_dotenv

from app.auth_service import provision_account
from app.meeting.models import AUTH_ACCOUNT_ROLES


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision local login for an existing employee.")
    parser.add_argument("--employee-email", required=True, help="Email of an existing active Employee")
    parser.add_argument(
        "--role",
        choices=AUTH_ACCOUNT_ROLES,
        default="employee",
        help="Server-managed role; defaults to employee",
    )
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Provisioning requires an interactive operator terminal.")

    if args.role != "employee":
        confirmation = input(f"To assign the elevated role '{args.role}', type it again: ").strip()
        if confirmation != args.role:
            parser.error("Role confirmation did not match; no account was created.")

    password = getpass.getpass("New password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match; no account was created.")

    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=False)
    from app.db.database import SessionLocal

    try:
        with SessionLocal() as db:
            account = provision_account(db, args.employee_email, password, args.role)
            employee_id = account.employee_id
    except ValueError as exc:
        parser.error(str(exc))

    print(
        f"Provisioned {args.role} account for {args.employee_email.strip()} "
        f"(employee_id={employee_id})."
    )


if __name__ == "__main__":
    main()