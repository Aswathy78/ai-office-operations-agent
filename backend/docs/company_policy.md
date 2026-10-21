# NorthStar Operations — Company Policy Reference

This document is the authoritative policy reference for the AI Office Operations Agent
(Story 6.4). It is used only when the assistant must cite or apply a policy, rule,
threshold, guideline, or procedure that requires company-specific context.

---

## Meeting Scheduling Policy

Meeting duration must be between 15 and 480 minutes (8 hours). The default duration is
30 minutes. Meetings that exceed 4 hours require manager approval.

All-hands meetings (tagged "all-hands" or "company meeting") require at least 48 hours
advance notice and must be approved by an HR Operations manager before invitations are
sent to the entire employee directory.

External guests (email addresses outside @northstar.example) may attend meetings but must
be admitted through the lobby approval process. The organizer or an authorised delegate
must explicitly admit each guest.

Recurring meetings are capped at 52 occurrences per series.

---

## Task Assignment Policy

Tasks must have a description and a named assignee. Anonymous task creation is not
permitted.

A task may only be reassigned to another employee by a manager, HR Operations, or an
admin. Employee self-reassignment is blocked.

Overdue tasks (past their due date) automatically trigger an escalation notification to
the line manager of the assigned employee within 24 hours.

---

## Email Drafting and Approval Policy

Emails addressed to more than 10 recipients, or to any recipient outside the
organisation, require manager or HR Operations approval before they can be sent. This is
the EMAIL_APPROVAL_RECIPIENT_THRESHOLD.

Sensitive-data scanning runs on every draft before queuing. Findings must be acknowledged
by the sender. Drafts with unacknowledged findings cannot be queued for delivery.

Bulk emails (to "everyone" or a directory group with more than 10 members) are always
subject to the approval gate regardless of the individual recipient count.

---

## Action Item Review Policy

Extracted action items must be reviewed by the meeting organiser or an authorised
delegate before tasks are created from them. Unreviewed action items do not generate tasks
or notifications automatically.

---

## Recording and Retention Policy

Meeting recordings are retained for 90 days. After that period the recording file and its
encryption key are permanently deleted. Early deletion can only be triggered by an admin.

Consent to record must be given by each participant individually. Participants who decline
consent are excluded from all recording tracks. Recordings that capture a participant who
has declined consent must be stopped immediately.

---

## Data and Security Policy

API keys, encryption keys, JWT secrets, and SMTP credentials must never appear in logs,
assistant responses, or API responses. The assistant must refuse any request that would
expose configuration secrets.

User conversations and their messages are private to the conversation owner. No employee
may read, modify, or delete another employee's conversation without an explicit admin
override.

---

## Budget and Cost Controls

The AI assistant operates within a configurable token budget per request, set via the
ASSISTANT_COST_CAP_TOKENS environment variable (default: 4000 tokens estimated). When the
budget is exhausted mid-plan, the current step is not started and execution halts. The
checkpoint for all completed steps is preserved.

The maximum number of plan steps per request is controlled by ASSISTANT_MAX_STEPS
(default: 12). Requests that would require more than 12 steps should be decomposed into
multiple separate requests.
