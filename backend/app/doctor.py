"""Check that this machine is set up to run the project, in plain words.

    python -m app.doctor          # checks only; never changes anything
    python -m app.doctor --mail   # also tries the e-mail login once (nothing is sent)

Each line starts with OK, WARN or FAIL and, when something is wrong, says how to fix it.
It exists because the same few set-up problems kept coming back as confusing errors: the
wrong Python environment, a database behind the code, seed passwords that do not match
the database, a mail password changed in ``.env`` but not loaded, and a missing OCR
engine. Exit status is 1 when anything FAILs, so scripts can stop early.
"""

from __future__ import annotations

import importlib.util
import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
PROJECT_DIRECTORY = BACKEND_DIRECTORY.parent
ENV_FILE = BACKEND_DIRECTORY / ".env"

# Imported name -> pip name, for everything the app needs to start and do its job.
REQUIRED_PACKAGES = {
    "fastapi": "fastapi",
    "sqlalchemy": "SQLAlchemy",
    "alembic": "alembic",
    "dotenv": "python-dotenv",
    "uvicorn": "uvicorn",
    "pwdlib": "pwdlib[argon2]",
    "jwt": "PyJWT",
    "cryptography": "cryptography",
    "groq": "groq",
    "faster_whisper": "faster-whisper",
    "fastembed": "fastembed",
    "pypdf": "pypdf",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "openpyxl": "openpyxl",
    "sqlite_vec": "sqlite-vec",
    "PIL": "Pillow",
    "pytesseract": "pytesseract",
    "filetype": "filetype",
    "watchfiles": "watchfiles",
}
TESTED_PYTHON = (3, 11)


@dataclass(frozen=True)
class Check:
    status: str  # "ok" | "warn" | "fail"
    title: str
    detail: str = ""
    fix: str = ""


def _ok(title: str, detail: str = "") -> Check:
    return Check("ok", title, detail)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python() -> list[Check]:
    checks = []
    version = sys.version_info
    label = f"Python {version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) != TESTED_PYTHON:
        checks.append(Check(
            "warn", f"{label} is not the version this project is tested with (3.11)",
            fix="Use the project's own environment: .venv-py311\\Scripts\\python.exe "
                "(or start everything with scripts\\dev.ps1).",
        ))
    else:
        checks.append(_ok(label))
    missing = [pip for module, pip in REQUIRED_PACKAGES.items() if importlib.util.find_spec(module) is None]
    if missing:
        checks.append(Check(
            "fail", "Packages missing from this Python environment: " + ", ".join(missing),
            "Anything run with this Python will fail when it needs them (uploads, transcription, search).",
            "Run the project with .venv-py311, or install them: "
            "python -m pip install -r backend\\requirements.txt",
        ))
    else:
        checks.append(_ok("All required Python packages are installed"))
    return checks


def check_database() -> list[Check]:
    from app.db.database import DATABASE_PATH, engine
    from app.db.migrate import current_revision, head_revision

    if not DATABASE_PATH.exists():
        return [Check("warn", f"No database file yet at {DATABASE_PATH}",
                      fix="It is created when the API starts.")]
    try:
        current, head = current_revision(engine), head_revision()
    except Exception as error:  # noqa: BLE001
        return [Check("fail", f"The database could not be read: {error}", fix=f"File: {DATABASE_PATH}")]
    if current is None:
        return [Check("warn", "The database has no migration history (it was created from the models)",
                      fix="Fine for a test database. For a real one run: cd backend; alembic upgrade head")]
    if current != head:
        return [Check("warn", f"The database is behind the code ({current}, latest is {head})",
                      "The API upgrades it automatically (with a backup) when it starts.",
                      "Restart the API, or run: cd backend; alembic upgrade head")]
    return [_ok(f"Database is up to date ({head})")]


def check_accounts() -> list[Check]:
    if os.environ.get("APP_ENV", "development").strip().lower() == "production":
        return []
    try:
        from app.auth_service import PASSWORD_HASHER, normalize_email
        from app.db.database import SessionLocal
        from app.db.seed_accounts import DEFAULT_ACCOUNTS
        from app.meeting.models import AuthAccount

        with SessionLocal() as db:
            accounts = {a.email_normalized: a for a in db.query(AuthAccount).all()}
    except Exception as error:  # noqa: BLE001
        return [Check("warn", f"Accounts could not be checked: {error}")]
    problems, working = [], 0
    for item in DEFAULT_ACCOUNTS:
        account = accounts.get(normalize_email(item["email"]))
        if account is None:
            problems.append(f"{item['email']} does not exist")
            continue
        try:
            matches = PASSWORD_HASHER.verify(item["password"], account.password_hash)
        except Exception:  # noqa: BLE001
            matches = False
        if not matches:
            problems.append(f"{item['email']} does not accept the password in seed_accounts.py")
        elif not account.is_enabled:
            problems.append(f"{item['email']} is disabled")
        else:
            working += 1
    if problems:
        return [Check("warn", "Demo accounts: " + "; ".join(problems),
                      fix="Reset them to the file's passwords: cd backend; python -m app.db.seed_accounts")]
    return [_ok(f"All {working} demo accounts accept the passwords in seed_accounts.py")]


def check_mail(try_login: bool = False) -> list[Check]:
    values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    if not ENV_FILE.exists():
        return [Check("warn", "backend/.env does not exist", fix="Copy backend/.env.example to backend/.env")]
    checks = []
    needed = ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM_EMAIL", "SMTP_USERNAME", "SMTP_PASSWORD")
    absent = [k for k in needed if not (values.get(k) or "").strip()]
    if absent:
        return [Check("warn", "E-mail is not set up: " + ", ".join(absent) + " missing in backend/.env",
                      "Notifications and e-mails will be recorded as failed.",
                      "Fill in the SMTP_ settings in backend/.env (see backend/.env.example).")]
    overriding = [k for k in values if k.startswith(("SMTP_", "FALLBACK_SMTP_")) and k in os.environ
                  and os.environ[k] != values[k]]
    if overriding:
        checks.append(Check(
            "fail", "A Windows/shell variable overrides backend/.env: " + ", ".join(overriding),
            "When the API starts it now prefers the file, but other tools prefer the variable.",
            "Remove it: in PowerShell run  Remove-Item Env:" + overriding[0] + "  and check "
            "System Properties > Environment Variables.",
        ))
    if values["SMTP_USERNAME"].strip().lower() != values["SMTP_FROM_EMAIL"].strip().lower():
        checks.append(Check("warn", "SMTP_FROM_EMAIL differs from SMTP_USERNAME",
                            "Gmail replaces a different sender with the login account.",
                            "Use the same address for both."))
    password = values["SMTP_PASSWORD"].strip().strip("\"'")
    if "gmail" in values["SMTP_HOST"].lower():
        compact = password.replace(" ", "")
        if len(compact) != 16 or not compact.isalpha():
            checks.append(Check(
                "warn", f"SMTP_PASSWORD does not look like a Google app password ({len(compact)} characters)",
                "An app password is 16 letters (Google shows them in groups of four).",
                "Create one: Google Account > Security > 2-Step Verification > App passwords. "
                "It must be made in the same account as SMTP_USERNAME.",
            ))
    if not checks:
        checks.append(_ok(f"Mail settings are consistent ({values['SMTP_USERNAME']} via {values['SMTP_HOST']})"))
    if try_login:
        checks.append(_try_mail_login())
    return checks


def _try_mail_login() -> Check:
    from app import email_providers
    from app.plain_errors import plain_error

    try:
        email_providers.check_smtp_connection(email_providers.primary_settings())
    except Exception as error:  # noqa: BLE001
        return Check("fail", "E-mail login failed: " + (plain_error(error, email=True) or "unknown error"))
    return _ok("E-mail login works (nothing was sent)")


def check_ocr() -> list[Check]:
    from app.documents.extraction import tesseract_path

    path = tesseract_path()
    if path:
        return [_ok(f"OCR engine found ({path})")]
    return [Check("warn", "The OCR engine (Tesseract) is not installed",
                  "Images and scanned PDFs cannot be read; ordinary text documents are fine.",
                  "Install it: winget install -e --id UB-Mannheim.TesseractOCR")]


def check_assistant_key() -> list[Check]:
    values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    if (values.get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip():
        return [_ok("AI assistant key (GROQ_API_KEY) is set")]
    return [Check("warn", "GROQ_API_KEY is not set",
                  "The assistant answers in a limited offline mode; summaries and action items fail.",
                  "Add GROQ_API_KEY to backend/.env and restart the API.")]


def check_frontend() -> list[Check]:
    modules = PROJECT_DIRECTORY / "node_modules"
    if not (modules / "react-markdown").exists():
        return [Check("fail", "The front end's packages are not installed (react-markdown is missing)",
                      "The pages will not build.", "From the project folder run: npm install")]
    package = PROJECT_DIRECTORY / "package.json"
    marker = modules / ".package-lock.json"
    if marker.exists() and package.stat().st_mtime > marker.stat().st_mtime:
        return [Check("warn", "package.json changed after the last npm install",
                      fix="From the project folder run: npm install")]
    return [_ok("Front-end packages are installed")]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def check_servers() -> list[Check]:
    api = [port for port in (8000, 8010) if _listening(port)]
    checks = []
    if len(api) > 1:
        checks.append(Check(
            "warn", f"Two API servers are running (ports {', '.join(map(str, api))})",
            "One may have older code or an older .env than the other, so results differ by "
            "which one the browser talks to.", "Stop one, or use scripts\\dev.ps1 which keeps one."))
    elif api:
        checks.append(_ok(f"API server running on port {api[0]}"))
    else:
        checks.append(Check("warn", "No API server is running", fix="Start it with scripts\\dev.ps1"))
    checks.append(_ok("Front end running on port 5173") if _listening(5173)
                  else Check("warn", "The front end is not running", fix="Start it with scripts\\dev.ps1"))
    return checks


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------


def run_checks(*, try_mail_login: bool = False) -> list[Check]:
    results: list[Check] = []
    for check in (check_python, check_database, check_accounts, lambda: check_mail(try_mail_login),
                  check_ocr, check_assistant_key, check_frontend, check_servers):
        try:
            results.extend(check())
        except Exception as error:  # noqa: BLE001 - one broken check must not hide the rest
            results.append(Check("warn", f"A check could not run: {type(error).__name__}: {error}"))
    return results


def format_report(checks: list[Check]) -> str:
    labels = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = []
    for check in checks:
        lines.append(f"{labels[check.status]}  {check.title}")
        if check.detail:
            lines.append(f"      {check.detail}")
        if check.fix:
            lines.append(f"      Fix: {check.fix}")
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    lines.append("")
    lines.append("Everything looks good." if not failed and not warned
                 else f"{failed} problem(s) and {warned} warning(s) found.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, str(BACKEND_DIRECTORY))
    checks = run_checks(try_mail_login="--mail" in argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(format_report(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
