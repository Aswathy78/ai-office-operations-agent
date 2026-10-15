"""E-mail drafting from chat (Story 4.3 / FR-20).

The assistant prepares an e-mail, but nothing is sent until the employee who
asked for it reviews the preview and explicitly confirms. Specifically:

- Drafts come from a fixed template when the request matches one, otherwise
  from the LLM (with a template fallback when the LLM is unavailable).
- Directory groups such as "the development team" resolve to individual
  employees, and the resolved count is returned with the preview.
- More than ``EMAIL_APPROVAL_RECIPIENT_THRESHOLD`` (default 10) recipients, or
  any recipient outside the organisation, sets ``approval_required``. Such a
  draft must be approved by a manager / HR / admin before it can be sent.
- A sensitive-data scan runs on every draft change and again immediately
  before sending; findings must be acknowledged by the sender.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import re
import smtplib
import uuid

from sqlalchemy.orm import Session

from app.meeting.models import (
    EMAIL_DRAFT_SOURCE_LLM,
    EMAIL_DRAFT_SOURCE_TEMPLATE,
    EMAIL_DRAFT_STATUS_APPROVED,
    EMAIL_DRAFT_STATUS_DISCARDED,
    EMAIL_DRAFT_STATUS_DRAFT,
    EMAIL_DRAFT_STATUS_FAILED,
    EMAIL_DRAFT_STATUS_PARTIALLY_SENT,
    EMAIL_DRAFT_STATUS_PENDING_APPROVAL,
    EMAIL_DRAFT_STATUS_REJECTED,
    EMAIL_DRAFT_STATUS_SENT,
    EmailDraft,
    Employee,
)

logger = logging.getLogger(__name__)

APPROVER_ROLES = frozenset({"manager", "hr_operations", "admin"})
EVERYONE_GROUP = "Everyone"
EDITABLE_STATUSES = frozenset({
    EMAIL_DRAFT_STATUS_DRAFT,
    EMAIL_DRAFT_STATUS_PENDING_APPROVAL,
    EMAIL_DRAFT_STATUS_APPROVED,
    EMAIL_DRAFT_STATUS_REJECTED,
})
# Statuses a send may start from; partially_sent / failed retry only undelivered recipients.
SENDABLE_STATUSES = frozenset({
    EMAIL_DRAFT_STATUS_DRAFT,
    EMAIL_DRAFT_STATUS_APPROVED,
    EMAIL_DRAFT_STATUS_PARTIALLY_SENT,
    EMAIL_DRAFT_STATUS_FAILED,
})

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAX_SUBJECT_LENGTH = 255
MAX_BODY_LENGTH = 20000


@dataclass
class EmailActor:
    id: str
    role: str
    team: str | None = None


class EmailDraftNotFound(Exception):
    """HTTP 404."""


class EmailDraftDenied(Exception):
    """The actor may not perform this action (HTTP 403)."""


class EmailDraftConflict(Exception):
    """The action conflicts with the draft's current state (HTTP 409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def approval_recipient_threshold() -> int:
    try:
        return max(1, int(os.getenv("EMAIL_APPROVAL_RECIPIENT_THRESHOLD", "10")))
    except ValueError:
        return 10


def organization_email_domains() -> set[str]:
    raw = os.getenv("ORGANIZATION_EMAIL_DOMAINS", "northstar.example")
    return {domain.strip().lower() for domain in raw.split(",") if domain.strip()}


# ---------------------------------------------------------------------------
# Directory groups and recipient resolution
# ---------------------------------------------------------------------------

# Extra words people use for a team, keyed by the directory team name (lower-case).
GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "engineering": ("development", "dev", "developers", "engineers", "tech", "engineering"),
    "finance": ("finance", "accounts", "accounting"),
    "operations": ("operations", "ops"),
}
EVERYONE_PATTERN = re.compile(
    r"\b(everyone|everybody|all[- ](staff|employees|hands)|whole company|"
    r"entire company|company[- ]wide)\b",
    re.IGNORECASE,
)


def _active_employees(db: Session) -> list[Employee]:
    return (
        db.query(Employee)
        .filter(Employee.is_active.is_(True))
        .order_by(Employee.name)
        .all()
    )


def list_directory_groups(db: Session) -> list[dict]:
    """Teams in the employee directory with their member counts."""
    employees = _active_employees(db)
    teams: dict[str, int] = {}
    for employee in employees:
        if employee.team:
            teams[employee.team] = teams.get(employee.team, 0) + 1
    groups = [{"name": name, "count": count} for name, count in sorted(teams.items())]
    groups.append({"name": EVERYONE_GROUP, "count": len(employees)})
    return groups


def _group_members(employees: list[Employee], group: str) -> list[Employee]:
    if group.casefold() == EVERYONE_GROUP.casefold():
        return list(employees)
    return [e for e in employees if (e.team or "").casefold() == group.casefold()]


def _group_patterns(team: str) -> list[re.Pattern]:
    aliases = set(GROUP_ALIASES.get(team.casefold(), ())) | {team.casefold()}
    alternation = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    return [
        # "the development team", "finance department", "ops group"
        re.compile(rf"\b({alternation})\s+(team|group|department|dept)\b", re.IGNORECASE),
        # "email engineering", "send it to finance"
        re.compile(
            rf"\b(to|email|e-mail|mail|notify|inform|tell|message|cc)\s+(the\s+)?({alternation})\b",
            re.IGNORECASE,
        ),
    ]


def detect_groups(db: Session, text: str) -> list[str]:
    """Directory groups mentioned in a chat request, e.g. 'the development team'."""
    if EVERYONE_PATTERN.search(text):
        return [EVERYONE_GROUP]
    teams = sorted({e.team for e in _active_employees(db) if e.team})
    return [team for team in teams if any(p.search(text) for p in _group_patterns(team))]


def detect_named_employees(db: Session, text: str) -> list[Employee]:
    """Employees mentioned by full name, or by a first name that is unique."""
    employees = _active_employees(db)
    first_name_counts: dict[str, int] = {}
    for employee in employees:
        first = employee.name.split()[0].casefold()
        first_name_counts[first] = first_name_counts.get(first, 0) + 1

    found: list[Employee] = []
    for employee in employees:
        full = re.compile(rf"\b{re.escape(employee.name)}\b", re.IGNORECASE)
        first = employee.name.split()[0]
        first_re = re.compile(rf"\b{re.escape(first)}\b", re.IGNORECASE)
        if full.search(text) or (
            first_name_counts[first.casefold()] == 1 and first_re.search(text)
        ):
            found.append(employee)
    return found


def _is_internal(email: str, directory_emails: set[str]) -> bool:
    email = email.casefold()
    if email in directory_emails:
        return True
    return email.rsplit("@", 1)[-1] in organization_email_domains()


def build_recipients(
    db: Session,
    *,
    groups: list[str],
    employees: list[Employee],
    emails: list[str],
    exclude_email: str | None,
    existing: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Merge groups, named employees and raw addresses into a de-duplicated list.

    Returns ``(recipients, recipient_groups, unresolved)``. The sender is left
    out of group expansion (they are writing to their team, not themselves).
    """
    directory = _active_employees(db)
    by_email = {e.email.casefold(): e for e in directory}
    directory_emails = set(by_email)
    exclude = (exclude_email or "").casefold()

    recipients: list[dict] = []
    seen: set[str] = set()

    def add(name: str, email: str, source: str) -> None:
        key = email.casefold()
        if key in seen:
            return
        seen.add(key)
        recipients.append({
            "name": name,
            "email": email,
            "internal": _is_internal(email, directory_emails),
            "source": source,
        })

    for item in existing or []:
        add(item["name"], item["email"], item.get("source") or "Added manually")

    resolved_groups: list[dict] = []
    unresolved: list[str] = []
    for group in groups:
        members = [m for m in _group_members(directory, group) if m.email.casefold() != exclude]
        if not members:
            unresolved.append(group)
            continue
        label = group if group == EVERYONE_GROUP else f"{group} team"
        resolved_groups.append({"name": label, "count": len(members)})
        for member in members:
            add(member.name, member.email, label)

    for employee in employees:
        add(employee.name, employee.email, "Named in request")

    for raw in emails:
        email = raw.strip().strip(".,;:<>()[]")
        if not EMAIL_PATTERN.fullmatch(email):
            unresolved.append(raw)
            continue
        employee = by_email.get(email.casefold())
        add(employee.name if employee else email, employee.email if employee else email,
            "Added manually")

    return recipients, resolved_groups, unresolved


# ---------------------------------------------------------------------------
# Approval rules
# ---------------------------------------------------------------------------


def evaluate_approval(recipients: list[dict]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    threshold = approval_recipient_threshold()
    if len(recipients) > threshold:
        reasons.append(
            f"{len(recipients)} recipients exceeds the limit of {threshold} "
            "for sending without approval."
        )
    external = [r["email"] for r in recipients if not r["internal"]]
    if external:
        reasons.append("External recipient(s): " + ", ".join(external) + ".")
    return bool(reasons), reasons


# ---------------------------------------------------------------------------
# Sensitive-data scan
# ---------------------------------------------------------------------------


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 4:
        return "•" * len(value)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]


# (type, label, severity, pattern, mask?)
SENSITIVE_PATTERNS: list[tuple[str, str, str, re.Pattern, bool]] = [
    ("private_key", "Private key", "high",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), False),
    ("api_key", "API key or access token", "high",
     re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|"
                r"ghp_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})\b"), True),
    ("password", "Password or PIN", "high",
     re.compile(r"\b(password|passwd|pwd|passcode|pin)\b\s*(is|:|=)\s*\S+", re.IGNORECASE), True),
    ("credit_card", "Payment card number", "high",
     re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"), True),
    ("national_id", "Aadhaar number", "high",
     re.compile(r"(?<![\d-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d-])"), True),
    ("national_id", "Social Security number", "high",
     re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"), True),
    ("tax_id", "PAN number", "high",
     re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), True),
    ("bank_account", "Bank account number", "high",
     re.compile(r"\b(account|a/c|acct|iban)\s*(no\.?|number|#)?\s*[:\-]?\s*(?=[A-Z0-9]*\d)[A-Z0-9]{9,34}\b",
                re.IGNORECASE), True),
    ("confidential", "Confidentiality marker", "medium",
     re.compile(r"\b(confidential|strictly private|do not (forward|share)|internal only|"
                r"under nda)\b", re.IGNORECASE), False),
    ("compensation", "Salary / compensation details", "medium",
     re.compile(r"\b(salary|salaries|compensation|payslip|pay slip|ctc|bonus amount)\b",
                re.IGNORECASE), False),
    ("health", "Health information", "medium",
     re.compile(r"\b(medical (record|condition|report|history)|diagnos(is|ed)|"
                r"sick note)\b", re.IGNORECASE), False),
    ("personal", "Date of birth", "medium",
     re.compile(r"\b(date of birth|d\.?o\.?b\.?)\b", re.IGNORECASE), False),
]


def scan_sensitive(subject: str, body: str) -> list[dict]:
    """Flag content that should not leave in an e-mail without a second look."""
    findings: list[dict] = []
    for field, text in (("subject", subject or ""), ("body", body or "")):
        claimed: list[tuple[int, int]] = []
        for kind, label, severity, pattern, mask in SENSITIVE_PATTERNS:
            for match in pattern.finditer(text):
                start, end = match.span()
                if any(start < c_end and end > c_start for c_start, c_end in claimed):
                    continue
                value = match.group(0)
                if kind == "credit_card":
                    digits = re.sub(r"\D", "", value)
                    if not (13 <= len(digits) <= 19 and _luhn_valid(digits)):
                        continue
                claimed.append((start, end))
                findings.append({
                    "type": kind,
                    "label": label,
                    "severity": severity,
                    "match": _mask(value) if mask else value,
                    "field": field,
                })
    return findings


# ---------------------------------------------------------------------------
# Drafting: templates and LLM
# ---------------------------------------------------------------------------

TOPIC_PATTERN = re.compile(r"\b(?:about|regarding|re:|on the topic of)\s+(.+)", re.IGNORECASE)

# Each template: (key, trigger pattern, subject, body). Bodies use {greeting},
# {topic} and {sender}; bracketed text is a placeholder the employee fills in.
TEMPLATES: list[tuple[str, re.Pattern, str, str]] = [
    (
        "out_of_office",
        re.compile(r"\b(out of office|ooo|on leave|annual leave|vacation|time off)\b", re.IGNORECASE),
        "Out of office: {topic_title}",
        "Hi {greeting},\n\n"
        "I wanted to let you know that I will be out of office ({topic}).\n\n"
        "During this time I will have limited access to e-mail. For anything urgent, "
        "please contact [backup contact].\n\n"
        "Thank you,\n{sender}",
    ),
    (
        "deadline_reminder",
        re.compile(r"\b(deadline|due date|due by|due on|submit by)\b", re.IGNORECASE),
        "Deadline reminder: {topic_title}",
        "Hi {greeting},\n\n"
        "This is a reminder about the upcoming deadline for {topic}.\n\n"
        "Please make sure your part is completed on time, and let me know as soon as "
        "possible if anything is blocking you.\n\n"
        "Thanks,\n{sender}",
    ),
    (
        "meeting_reminder",
        re.compile(r"\b(remind\w*)\b.*\b(meeting|call|sync|stand-?up|demo|review)\b|"
                   r"\b(meeting|call|sync|stand-?up|demo|review)\b.*\breminder\b",
                   re.IGNORECASE),
        "Reminder: {topic_title}",
        "Hi {greeting},\n\n"
        "This is a friendly reminder about {topic}.\n\n"
        "Please plan to attend and come prepared. Let me know if you have any questions.\n\n"
        "Best regards,\n{sender}",
    ),
]

GENERAL_TEMPLATE = (
    "general",
    "{topic_title}",
    "Hi {greeting},\n\n"
    "I am writing regarding {topic}.\n\n"
    "[Add the details you want to share here.]\n\n"
    "Best regards,\n{sender}",
)


COMMAND_PREFIX = re.compile(
    r"^\s*(please\s+)?(can you\s+)?(draft|write|send|compose|prepare|e-?mail|mail)\b"
    r"(\s+(an?|the))?(\s+e-?mail)?(\s+to)?\s*",
    re.IGNORECASE,
)
AUDIENCE_PHRASE = re.compile(
    r"\b(to\s+)?(the\s+)?\w+\s+(team|group|department|dept)\b\s*|\beveryone\b\s*",
    re.IGNORECASE,
)


def extract_topic(prompt: str) -> str:
    match = TOPIC_PATTERN.search(prompt)
    if match:
        topic = match.group(1)
    else:
        topic = AUDIENCE_PHRASE.sub("", COMMAND_PREFIX.sub("", prompt))
    topic = re.sub(r"\s+", " ", topic).strip().rstrip(".!?").strip()
    return topic[:150] or "the matter we discussed"


def _greeting(recipients: list[dict], groups: list[dict]) -> str:
    if len(recipients) == 1:
        return recipients[0]["name"].split()[0] if "@" not in recipients[0]["name"] else "there"
    if len(groups) == 1 and len(recipients) == groups[0]["count"]:
        return "everyone" if groups[0]["name"] == EVERYONE_GROUP else groups[0]["name"]
    return "all"


def match_template(prompt: str) -> tuple[str, re.Pattern, str, str] | None:
    for template in TEMPLATES:
        if template[1].search(prompt):
            return template
    return None


def _render_template(subject: str, body: str, *, topic: str, greeting: str, sender: str):
    values = {
        "topic": topic,
        "topic_title": topic[:1].upper() + topic[1:],
        "greeting": greeting,
        "sender": sender,
    }
    rendered_subject = subject.format(**values)
    return rendered_subject[:1].upper() + rendered_subject[1:], body.format(**values)


def generate_with_llm(prompt: str, *, sender: str, greeting: str) -> tuple[str, str]:
    """Ask the configured LLM for ``(subject, body)``. Raises on any failure."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    from groq import Groq

    client = Groq(api_key=api_key, timeout=30)
    model = os.environ.get("GROQ_EMAIL_MODEL") or os.environ.get(
        "GROQ_SUMMARY_MODEL", "llama-3.3-70b-versatile"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You draft short, professional workplace e-mails for an employee. "
                    "Write only what the request supports: do not invent dates, times, "
                    "figures, names or commitments. Where a needed detail is missing, "
                    "insert a placeholder in square brackets, e.g. [date]. Never add "
                    "passwords, personal identifiers or financial data. The body is "
                    "plain text: start with greeting_line exactly as given, then the "
                    "message, then a sign-off with the sender's name. Do not add "
                    "deadlines, meeting times or requests the employee did not ask "
                    "for. Respond with valid JSON "
                    'with exactly two string fields: "subject" and "body".'
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "request": prompt,
                    "sender_name": sender,
                    "greeting_line": f"Hi {greeting},",
                }),
            },
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("The model returned an empty draft")
    parsed = json.loads(content)
    subject = str(parsed.get("subject") or "").strip()
    body = str(parsed.get("body") or "").strip()
    if not subject or not body:
        raise RuntimeError("The model's draft was missing a subject or body")
    return subject[:MAX_SUBJECT_LENGTH], body[:MAX_BODY_LENGTH]


def compose_draft(
    prompt: str,
    *,
    mode: str,
    sender: str,
    recipients: list[dict],
    groups: list[dict],
) -> dict:
    """Return ``{source, template_key, subject, body, generation_note}``."""
    topic = extract_topic(prompt)
    greeting = _greeting(recipients, groups)
    template = match_template(prompt) if mode in ("auto", "template") else None

    if template is not None:
        key, _, subject, body = template
        subject, body = _render_template(subject, body, topic=topic, greeting=greeting, sender=sender)
        return {"source": EMAIL_DRAFT_SOURCE_TEMPLATE, "template_key": key,
                "subject": subject, "body": body, "generation_note": None}

    note = None
    if mode != "template":
        try:
            subject, body = generate_with_llm(prompt, sender=sender, greeting=greeting)
            return {"source": EMAIL_DRAFT_SOURCE_LLM, "template_key": None,
                    "subject": subject, "body": body, "generation_note": None}
        except Exception as exc:  # noqa: BLE001 - any LLM failure falls back to a template
            logger.warning("LLM e-mail drafting failed; using template: %s", exc)
            note = ("AI drafting is unavailable right now, so a standard template was used. "
                    "Please fill in the bracketed details before sending.")
    else:
        note = "No specific template matched this request, so the general template was used."

    key, subject, body = GENERAL_TEMPLATE
    subject, body = _render_template(subject, body, topic=topic, greeting=greeting, sender=sender)
    return {"source": EMAIL_DRAFT_SOURCE_TEMPLATE, "template_key": key,
            "subject": subject[:MAX_SUBJECT_LENGTH], "body": body, "generation_note": note}


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


def _refresh_checks(draft: EmailDraft) -> None:
    draft.approval_required, draft.approval_reasons = evaluate_approval(draft.recipients)
    draft.sensitive_findings = scan_sensitive(draft.subject, draft.body)


def create_draft_from_chat(
    db: Session,
    actor: EmailActor,
    *,
    prompt: str,
    sender_name: str,
    sender_email: str | None,
    mode: str = "auto",
) -> EmailDraft:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Describe the e-mail you want to send.")

    groups = detect_groups(db, prompt)
    named = detect_named_employees(db, prompt)
    if sender_email:
        named = [e for e in named if e.email.casefold() != sender_email.casefold()]
    emails = EMAIL_PATTERN.findall(prompt)
    recipients, resolved_groups, unresolved = build_recipients(
        db, groups=groups, employees=named, emails=emails, exclude_email=sender_email,
    )

    # Draft from the prompt without the raw addresses so they don't end up in the topic.
    drafting_prompt = EMAIL_PATTERN.sub("", prompt)
    content = compose_draft(
        drafting_prompt, mode=mode, sender=sender_name,
        recipients=recipients, groups=resolved_groups,
    )

    now = _now()
    draft = EmailDraft(
        id=str(uuid.uuid4()),
        created_by_id=actor.id,
        created_by_name=sender_name,
        created_by_email=sender_email,
        created_by_role=actor.role,
        prompt=prompt,
        source=content["source"],
        template_key=content["template_key"],
        generation_note=content["generation_note"],
        subject=content["subject"],
        body=content["body"],
        recipients=recipients,
        recipient_groups=resolved_groups,
        unresolved_recipients=unresolved,
        status=EMAIL_DRAFT_STATUS_DRAFT,
        delivery_results=[],
        created_at=now,
        updated_at=now,
    )
    _refresh_checks(draft)
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def get_draft(db: Session, draft_id: str, actor: EmailActor) -> EmailDraft:
    draft = db.get(EmailDraft, draft_id)
    if draft is None:
        raise EmailDraftNotFound("E-mail draft not found.")
    if draft.created_by_id != actor.id and actor.role not in APPROVER_ROLES:
        raise EmailDraftDenied("You can only view your own e-mail drafts.")
    return draft


def _require_owner(draft: EmailDraft, actor: EmailActor) -> None:
    if draft.created_by_id != actor.id:
        raise EmailDraftDenied("Only the employee who created this draft can change or send it.")


def update_draft(
    db: Session,
    draft_id: str,
    actor: EmailActor,
    *,
    subject: str | None = None,
    body: str | None = None,
    recipient_emails: list[str] | None = None,
    add_groups: list[str] | None = None,
) -> EmailDraft:
    """Apply the employee's edits. Any edit invalidates a previous approval."""
    draft = get_draft(db, draft_id, actor)
    _require_owner(draft, actor)
    if draft.status not in EDITABLE_STATUSES:
        raise EmailDraftConflict(f"A draft that is {draft.status.replace('_', ' ')} can no longer be edited.")

    if subject is not None:
        draft.subject = subject.strip()[:MAX_SUBJECT_LENGTH]
    if body is not None:
        draft.body = body[:MAX_BODY_LENGTH]

    if recipient_emails is not None or add_groups:
        current = {r["email"].casefold(): r for r in draft.recipients}
        kept: list[dict] = []
        new_emails: list[str] = []
        for email in recipient_emails if recipient_emails is not None else [r["email"] for r in draft.recipients]:
            existing = current.get(email.strip().casefold())
            if existing is not None:
                kept.append(existing)
            else:
                new_emails.append(email)
        recipients, resolved_groups, unresolved = build_recipients(
            db,
            groups=add_groups or [],
            employees=[],
            emails=new_emails,
            exclude_email=draft.created_by_email,
            existing=kept,
        )
        if unresolved:
            raise ValueError("Not a valid e-mail address or directory group: " + ", ".join(unresolved))
        # Keep only group summaries whose members are still on the list, with live counts.
        all_groups = {g["name"]: g for g in draft.recipient_groups}
        all_groups.update({g["name"]: g for g in resolved_groups})
        draft.recipient_groups = [
            {"name": name, "count": count}
            for name in all_groups
            if (count := sum(1 for r in recipients if r["source"] == name))
        ]
        draft.recipients = recipients
        draft.unresolved_recipients = []

    if draft.status != EMAIL_DRAFT_STATUS_DRAFT:
        draft.status = EMAIL_DRAFT_STATUS_DRAFT
        draft.decided_by = None
        draft.decided_at = None
        draft.decision_note = None
    _refresh_checks(draft)
    draft.updated_at = _now()
    db.commit()
    db.refresh(draft)
    return draft


def submit_for_approval(db: Session, draft_id: str, actor: EmailActor) -> EmailDraft:
    draft = get_draft(db, draft_id, actor)
    _require_owner(draft, actor)
    _refresh_checks(draft)
    if not draft.approval_required:
        raise EmailDraftConflict("This e-mail does not need approval; you can send it directly.")
    if draft.status not in (EMAIL_DRAFT_STATUS_DRAFT, EMAIL_DRAFT_STATUS_REJECTED):
        raise EmailDraftConflict(f"A draft that is {draft.status.replace('_', ' ')} cannot be submitted.")
    _validate_content(draft)
    draft.status = EMAIL_DRAFT_STATUS_PENDING_APPROVAL
    draft.decided_by = None
    draft.decided_at = None
    draft.decision_note = None
    draft.updated_at = _now()
    db.commit()
    db.refresh(draft)
    return draft


def decide_draft(
    db: Session,
    draft_id: str,
    actor: EmailActor,
    *,
    decision: str,
    note: str | None,
) -> EmailDraft:
    draft = db.get(EmailDraft, draft_id)
    if draft is None:
        raise EmailDraftNotFound("E-mail draft not found.")
    if actor.role not in APPROVER_ROLES:
        raise EmailDraftDenied("Only managers, HR / Operations or admins can approve e-mails.")
    if draft.created_by_id == actor.id:
        raise EmailDraftDenied("You cannot approve your own e-mail.")
    if draft.status != EMAIL_DRAFT_STATUS_PENDING_APPROVAL:
        raise EmailDraftConflict("This e-mail is not waiting for approval.")
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'.")

    draft.status = EMAIL_DRAFT_STATUS_APPROVED if decision == "approve" else EMAIL_DRAFT_STATUS_REJECTED
    draft.decided_by = actor.id
    draft.decided_at = _now()
    draft.decision_note = (note or "").strip() or None
    draft.updated_at = draft.decided_at
    db.commit()
    db.refresh(draft)
    return draft


def _validate_content(draft: EmailDraft) -> None:
    if not draft.recipients:
        raise EmailDraftConflict("Add at least one recipient before sending.")
    if not draft.subject.strip():
        raise EmailDraftConflict("Add a subject before sending.")
    if not draft.body.strip():
        raise EmailDraftConflict("The e-mail body is empty.")
    if draft.unresolved_recipients:
        raise EmailDraftConflict(
            "Some recipients could not be resolved: " + ", ".join(draft.unresolved_recipients)
            + ". Remove or correct them before sending."
        )


def send_draft(
    db: Session,
    draft_id: str,
    actor: EmailActor,
    *,
    confirm: bool,
    acknowledge_sensitive: bool,
) -> EmailDraft:
    """Send the previewed e-mail. Only the creator can send, and only on explicit confirmation."""
    from app.task_service import send_email  # local import keeps SMTP config in one place

    draft = get_draft(db, draft_id, actor)
    _require_owner(draft, actor)
    if not confirm:
        raise ValueError("Sending requires explicit confirmation of the previewed e-mail.")
    if draft.status not in SENDABLE_STATUSES:
        if draft.status == EMAIL_DRAFT_STATUS_PENDING_APPROVAL:
            raise EmailDraftConflict("This e-mail is still waiting for approval.")
        raise EmailDraftConflict(f"A draft that is {draft.status.replace('_', ' ')} cannot be sent.")
    _validate_content(draft)

    # Re-run both checks against exactly what is about to go out.
    _refresh_checks(draft)
    already_approved = draft.status in (
        EMAIL_DRAFT_STATUS_APPROVED,
        EMAIL_DRAFT_STATUS_PARTIALLY_SENT,
        EMAIL_DRAFT_STATUS_FAILED,
    ) and draft.decided_at is not None
    if draft.approval_required and not already_approved:
        db.commit()
        raise EmailDraftConflict(
            "Approval is required before sending: " + " ".join(draft.approval_reasons)
        )
    if draft.sensitive_findings and not acknowledge_sensitive:
        db.commit()
        labels = sorted({f["label"] for f in draft.sensitive_findings})
        raise EmailDraftConflict(
            "Possible sensitive data found (" + ", ".join(labels) + "). "
            "Review the flagged items and confirm to send anyway."
        )

    delivered = {
        r["email"].casefold() for r in draft.delivery_results if r["status"] == "sent"
    }
    results = [r for r in draft.delivery_results if r["email"].casefold() in delivered]
    for recipient in draft.recipients:
        if recipient["email"].casefold() in delivered:
            continue
        try:
            send_email(recipient["email"], draft.subject, draft.body)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            logger.exception("E-mail draft delivery failed draft_id=%s recipient=%s",
                             draft.id, recipient["email"])
            results.append({"email": recipient["email"], "status": "failed", "error": str(exc)[:500]})
        else:
            results.append({"email": recipient["email"], "status": "sent", "error": None})

    sent_count = sum(1 for r in results if r["status"] == "sent")
    if sent_count == len(draft.recipients):
        draft.status = EMAIL_DRAFT_STATUS_SENT
    elif sent_count:
        draft.status = EMAIL_DRAFT_STATUS_PARTIALLY_SENT
    else:
        draft.status = EMAIL_DRAFT_STATUS_FAILED
    draft.delivery_results = results
    if sent_count:
        draft.sent_at = _now()
    draft.updated_at = _now()
    db.commit()
    db.refresh(draft)
    return draft


def discard_draft(db: Session, draft_id: str, actor: EmailActor) -> EmailDraft:
    draft = get_draft(db, draft_id, actor)
    _require_owner(draft, actor)
    if draft.status not in EDITABLE_STATUSES:
        raise EmailDraftConflict(f"A draft that is {draft.status.replace('_', ' ')} cannot be discarded.")
    draft.status = EMAIL_DRAFT_STATUS_DISCARDED
    draft.updated_at = _now()
    db.commit()
    db.refresh(draft)
    return draft


def list_my_drafts(db: Session, actor: EmailActor, limit: int = 20) -> list[EmailDraft]:
    return (
        db.query(EmailDraft)
        .filter(EmailDraft.created_by_id == actor.id)
        .filter(EmailDraft.status != EMAIL_DRAFT_STATUS_DISCARDED)
        .order_by(EmailDraft.updated_at.desc())
        .limit(limit)
        .all()
    )


def list_approval_queue(db: Session, actor: EmailActor, status: str | None) -> list[EmailDraft]:
    if actor.role not in APPROVER_ROLES:
        raise EmailDraftDenied("Only managers, HR / Operations or admins can review e-mail approvals.")
    query = db.query(EmailDraft).filter(EmailDraft.approval_required.is_(True))
    if status:
        query = query.filter(EmailDraft.status == status)
    else:
        query = query.filter(EmailDraft.status != EMAIL_DRAFT_STATUS_DISCARDED)
    return query.order_by(EmailDraft.updated_at.desc()).all()
