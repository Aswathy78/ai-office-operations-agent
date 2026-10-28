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

---

# Northwind Operations Pvt. Ltd. — HR & Operations Knowledge Handbook v2.1
Document ID: NW-HR-OPS-KB-002
Version: 2.1 (effective 01 Oct 2026)
Document Owner: Anita Menon, Head of HR & Operations
Classification: Internal - Confidential
Access list (ACL): HR Team (edit); Operations Team (read); Department Managers (read); AI Office Agent (read, index)
Purpose: Single source of company knowledge for employees and for the AI Office Operations Agent.

## 1. Company Overview and Working Hours
Northwind Operations is a 420-person services company headquartered in Kochi, Kerala, with delivery centres in Bengaluru and Pune. It provides business-process, logistics-coordination and analytics services to clients in retail, healthcare and manufacturing.
- Founded: 2011
- Headcount: 420 employees, 38 contractors
- Working hours: Monday to Friday, 09:30-18:00 IST (flexible core hours 11:00-16:00)
- Departments: Delivery, Finance, HR, IT, Legal, Operations, Sales
- Fiscal year: 1 April - 31 March

## 2. Onboarding and Offboarding Policy
New joiners should be productive within their first two weeks. HR coordinates the process; the hiring manager owns the role-specific plan.
- Onboarding timeline:
  - T-7 days: Offer accepted; background verification started; documents requested (ID proof, education certificates, previous employer relieving letter) by HR Executive.
  - T-3 days: Laptop, email account and building access prepared by IT Helpdesk.
  - Day 1: Welcome session, policy acknowledgement, payroll and PF forms signed with HR Executive.
  - Day 2-5: Role training, buddy assigned, intro to tools (HRMS, ticketing, knowledge base) with Hiring Manager.
  - Day 30: Check-in conversation and feedback form with HR Business Partner.
  - Day 90: Probation review and confirmation decision with Hiring Manager and HR.
- Documents collected from new joiners: Government ID and address proof, educational and professional certificates, signed offer letter, NDA, code-of-conduct acknowledgement, bank details and tax forms.
- Offboarding checklist:
  - Resignation accepted by manager and HR; notice period is 60 days (30 days during probation).
  - Knowledge-transfer plan agreed and documents handed to a new owner so that no document is left without an owner.
  - IT revokes access on the last working day; laptop and ID card returned.
  - Full-and-final settlement processed within 45 days; experience letter issued by HR.

## 3. Leave, Attendance and Holidays Policy
Leave is requested in the HRMS and approved by the reporting manager within two working days.
- Leave entitlements (per calendar year):
  - Earned leave: 18 days (accrues 1.5 days per month; up to 30 days carried forward).
  - Casual leave: 8 days (maximum 3 consecutive days; no carry forward).
  - Sick leave: 8 days (medical certificate needed for more than 2 consecutive days).
  - Maternity leave: 26 weeks (as per statutory provisions; HR to be informed 8 weeks in advance).
  - Paternity leave: 10 working days (to be used within 6 months of birth).
  - Bereavement leave: 5 days (immediate family; no documentation required).
  - Compensatory off: As earned (granted for approved work on weekends or holidays; expires in 60 days).
- Attendance and work from home (WFH):
  - Employees may work from home up to 8 days per month with manager approval.
  - Late arrivals beyond 3 times a month are marked as half-day unless regularised in the HRMS.
  - Attendance regularisation requests must be raised within 3 working days.
- Company holidays 2026-27 (selected):
  - 02 Oct 2026: Gandhi Jayanti
  - 20 Oct 2026: Vijayadashami
  - 08 Nov 2026: Diwali
  - 25 Dec 2026: Christmas
  - 26 Jan 2027: Republic Day
  - 14 Apr 2027: Vishu / Ambedkar Jayanti

## 4. Expenses, Travel and Procurement Policy
Expense claims must be submitted within 30 days of the spend with original or scanned receipts attached. Claims without receipts above INR 500 are rejected.
- Approval matrix:
  - Up to INR 10,000: Reporting Manager (turnaround 2 working days)
  - INR 10,001 - 50,000: Department Head (turnaround 3 working days)
  - INR 50,001 - 2,00,000: Finance Controller (turnaround 5 working days)
  - Above INR 2,00,000: CFO and COO (turnaround 7 working days)
- Travel policy:
  - Domestic flights: economy class, booked at least 7 days in advance through travel desk.
  - Hotel cap: INR 6,500 per night in metro cities, INR 4,500 elsewhere.
  - Daily allowance: INR 1,200 for domestic travel; actual meals reimbursed for client entertainment with approval.
  - Personal vehicle use: reimbursed at INR 9 per km.
- Vendor onboarding and procurement:
  - New vendors submit GST certificate, PAN, bank proof and signed NDA; Operations verifies and stores them.
  - Purchases above INR 1,00,000 require three quotations or a documented single-source justification.
  - Purchase orders are raised by Operations; invoices are paid within 30 days of approval.
  - Vendor performance is reviewed every quarter on delivery time, quality and responsiveness.

## 5. IT Access and Asset Management Policy
IT access follows the principle of least privilege: employees receive only the access needed for their role.
- Standard access by role:
  - All employees: Email, HRMS, intranet, knowledge base, ticketing (Approval for extra: Manager).
  - HR: HRMS admin, payroll viewer, document repository HR folders (Approval for extra: HR Head).
  - Operations: Procurement portal, vendor repository, document repository Ops folders (Approval for extra: Ops Head).
  - Finance: ERP, payment gateway view, expense approval tool (Approval for extra: Finance Controller).
  - Managers: Team dashboards, leave approval, budget reports (Approval for extra: Department Head).
- Asset management:
  - Every laptop and phone has an asset tag recorded in the asset register.
  - Lost or stolen devices must be reported to IT Helpdesk within 24 hours; device is remotely locked.
  - Laptops are refreshed every 4 years; requests for early replacement need Department Head approval.
  - Disk encryption and screen lock after 5 minutes are mandatory.
- IT Service Levels (SLA):
  - Priority P1 (Email or HRMS outage): Response within 30 minutes, Resolution within 4 hours.
  - Priority P2 (Laptop will not boot): Response within 2 hours, Resolution within 1 working day.
  - Priority P3 (Software installation request): Response within 1 working day, Resolution within 3 working days.

## 6. Document Management Policy: Owner and ACL
Company knowledge is findable, trustworthy and protected. Documents are uploaded to the company repository feeding the AI Office Operations Agent.
- Supported formats: PDF (.pdf), Word (.docx), PowerPoint (.pptx), Excel (.xlsx), Images (.png, .jpg, .jpeg, .tiff).
- Mandatory metadata before indexing:
  - Owner: a named employee accountable for accuracy. Departed employees' documents must be reassigned within 7 days.
  - Access Control List (ACL): users, groups or roles allowed to read the document. Default is owner only until uploader selects more.
  - Classification: Public, Internal, Confidential or Restricted.
  - Review date: when content must be re-validated.
- Upload statuses:
  - Pending: Uploaded, waiting for owner/ACL or processing (Next action: Complete metadata).
  - Processing: Parsing and OCR in progress (Next action: None).
  - Indexed: Text searchable by the agent within ACL limits (Next action: None).
  - Failed: Parsing or OCR failed; reason displayed (e.g., password-protected file, corrupt file, unsupported format, image too blurry).

## 7. Scanning and OCR Procedure
HR and Operations records (signed contracts, ID proofs, delivery challans, vendor invoices) must be scanned so text becomes searchable.
- Standards: Scan at 300 DPI minimum, colour for signatures/stamps, grayscale for plain text. Name: DEPT_DocType_PartyName_YYYY-MM-DD (e.g. HR_OfferLetter_RahulNair_2026-09-14.pdf). Maximum upload size 50 MB per file.
- Processing pipeline: 1. Upload -> 2. Detect -> 3. OCR (English and Malayalam supported) -> 4. Quality check -> 5. Index -> 6. Notify.
- Common failure reasons and fixes:
  - Password protected: Remove password and re-upload.
  - Corrupt file: Re-scan or re-export file.
  - Unsupported format: Convert to PDF or DOCX.
  - OCR confidence low: Re-scan at 300 DPI.
  - File too large: Over 50 MB; compress or split file.

## 8. Recurring Activities Calendar
- Monthly activities:
  - 1st working day: Attendance lock and leave balance update (Owner: HR Executive).
  - 5th: Payroll inputs frozen (Owner: Payroll Lead).
  - 7th: Salary credited (Owner: Finance).
  - 10th: Expense claims for previous month closed (Owner: Finance).
  - 15th: Vendor invoice payment run (Owner: Finance).
  - Last Friday: Town hall and policy updates (Owner: HR Head).
  - Last working day: Document repository review for missing owners or ACLs (Owner: Operations).
- Quarterly and annual activities:
  - Quarterly: Vendor performance review (Operations Head); Access rights recertification (IT Head); Fire drill and safety audit (Facilities).
  - April: Annual appraisal cycle begins (HR Head).
  - June: Statutory compliance audit (Legal).
  - October: Policy handbook review and re-acknowledgement (HR Head).
  - December: Business continuity test (Operations Head).
- Upcoming dates (Q4 2026):
  - 30 Oct 2026: Last day to submit September travel claims.
  - 13 Nov 2026: Access rights recertification deadline.
  - 27 Nov 2026: Town hall - new hybrid-work policy.
  - 15 Dec 2026: Document owners confirm review dates for all Confidential files.

## 9. Frequently Asked Questions (FAQ)
- Q: How do I apply for leave?
  A: Raise a request in the HRMS under Leave > Apply. Your manager approves it within two working days.
- Q: Can I upload a photo of a document taken on my phone?
  A: Yes, JPG or PNG images are accepted and processed through OCR. Make sure the page is flat and well lit.
- Q: Why is my uploaded document not searchable?
  A: Check its status. If it is Pending, add an owner and ACL. If it is Failed, read the reason and re-upload.
- Q: Who can see my document?
  A: Only people or groups on its ACL. The AI agent applies the same rule when answering questions.
- Q: What if the document owner leaves the company?
  A: HR reassigns ownership to the manager or a named delegate within 7 days of the last working day.
- Q: How long are records kept?
  A: Employee records: 7 years after exit. Vendor contracts: 8 years after expiry. Expense receipts: 8 years.

## 10. Contacts, Escalation and Revision History
- HR Helpdesk: Priya Nair (hr@northwind-ops.example, 09:30-18:00)
- Operations Helpdesk: Arjun Das (ops@northwind-ops.example, 09:00-19:00)
- IT Helpdesk: Fatima Khan (it@northwind-ops.example, 24x7 for P1)
- Finance: Suresh Iyer (finance@northwind-ops.example, 09:30-18:00)
- Escalation path: Helpdesk -> Department Head -> Head of HR & Operations (Anita Menon).
- Revision history:
  - v1.0 (03 Jan 2026): First release by A. Menon.
  - v2.0 (01 Apr 2026): Added expense approval matrix and IT access table by A. Menon.
  - v2.1 (01 Oct 2026): Added document owner/ACL policy and OCR procedure by A. Menon.
