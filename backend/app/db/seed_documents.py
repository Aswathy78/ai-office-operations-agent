"""Seed initial company documents into the database for RAG (Epic 7).

Preloads the Northwind Operations HR & Operations Knowledge Handbook v2.1
so the AI Assistant and Corporate Chatbox can answer company policy questions immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
import uuid

from sqlalchemy.orm import Session

from app.documents.models import DOC_STATUS_READY, Document
from app.documents.storage import write_document_file
from app.meeting.models import Employee

logger = logging.getLogger(__name__)

HANDBOOK_FILENAME = "HR_and_Operations_Knowledge_Handbook_v2.1.pdf"
HANDBOOK_STORED_NAME = "seed_handbook_v2_1.pdf"

HANDBOOK_TEXT = """HR & Operations Knowledge Handbook
Northwind Operations Pvt. Ltd.
Document ID: NW-HR-OPS-KB-002
Version: 2.1 (effective 01 Oct 2026)
Document owner: Anita Menon, Head of HR & Operations
Classification: Internal - Confidential
Access list (ACL): HR Team (edit); Operations Team (read); Department Managers (read); AI Office Agent (read, index)
Purpose: Single source of company knowledge for employees and for the AI Office Operations Agent

About this document:
This handbook consolidates the policies, procedures and recurring activities that HR and Operations teams use every day. It is written so that both people and the AI Office Operations Agent can answer questions such as How many casual leaves do I get?, Who approves a laptop request? or How do I upload a signed contract? Every section carries an owner so that stale content can be traced and corrected quickly.

1. Company Overview and Contents
Northwind Operations is a 420-person services company headquartered in Kochi, Kerala, with delivery centres in Bengaluru and Pune. It provides business-process, logistics-coordination and analytics services to clients in retail, healthcare and manufacturing.
- Founded: 2011
- Headcount: 420 employees, 38 contractors
- Working hours: Mon-Fri, 09:30-18:00 IST (flexible core hours 11:00-16:00)
- Departments: Delivery, Finance, HR, IT, Legal, Operations, Sales
- Fiscal year: 1 April - 31 March

2. Onboarding and Offboarding
New joiners should be productive within their first two weeks. HR coordinates the process; the hiring manager owns the role-specific plan.
2.1 Onboarding timeline:
- T-7 days: Offer accepted; background verification started; documents requested (ID proof, education certificates, previous employer relieving letter) - Responsible: HR Executive
- T-3 days: Laptop, email account and building access prepared - Responsible: IT Helpdesk
- Day 1: Welcome session, policy acknowledgement, payroll and PF forms signed - Responsible: HR Executive
- Day 2-5: Role training, buddy assigned, intro to tools (HRMS, ticketing, knowledge base) - Responsible: Hiring Manager
- Day 30: Check-in conversation and feedback form - Responsible: HR Business Partner
- Day 90: Probation review and confirmation decision - Responsible: Hiring Manager + HR
2.2 Documents collected from new joiners:
- Government ID proof and address proof (scanned copies are accepted and stored as restricted documents).
- Educational and professional certificates (verified against originals).
- Signed offer letter, NDA and code-of-conduct acknowledgement.
- Bank details and tax declaration forms (access limited to HR and Payroll).
2.3 Offboarding checklist:
- Resignation accepted by manager and HR; notice period is 60 days (30 days during probation).
- Knowledge-transfer plan agreed and documents handed to a new owner so that no document is left without an owner.
- IT revokes access on the last working day; laptop and ID card returned.
- Full-and-final settlement processed within 45 days; experience letter issued by HR.

3. Leave, Attendance and Holidays
Leave is requested in the HRMS and approved by the reporting manager within two working days.
3.1 Leave entitlements (per calendar year):
- Earned leave: 18 days (Accrues 1.5 days per month; up to 30 days carried forward).
- Casual leave: 8 days (Maximum 3 consecutive days; no carry forward).
- Sick leave: 8 days (Medical certificate needed for more than 2 consecutive days).
- Maternity leave: 26 weeks (As per statutory provisions; HR to be informed 8 weeks in advance).
- Paternity leave: 10 working days (To be used within 6 months of birth).
- Bereavement leave: 5 days (Immediate family; no documentation required).
- Compensatory off: As earned (Granted for approved work on weekends or holidays; expires in 60 days).
3.2 Attendance and work from home:
- Employees may work from home up to 8 days per month with manager approval.
- Late arrivals beyond 3 times a month are marked as half-day unless regularised in the HRMS.
- Attendance regularisation requests must be raised within 3 working days.
3.3 Company holidays 2026-27 (selected):
- 02 Oct 2026: Gandhi Jayanti
- 20 Oct 2026: Vijayadashami
- 08 Nov 2026: Diwali
- 25 Dec 2026: Christmas
- 26 Jan 2027: Republic Day
- 14 Apr 2027: Vishu / Ambedkar Jayanti

4. Expenses, Travel and Procurement
Expense claims must be submitted within 30 days of the spend with original or scanned receipts attached. Claims without receipts above INR 500 are rejected.
4.1 Approval matrix:
- Up to INR 10,000: Reporting Manager (turnaround 2 working days)
- INR 10,001 - 50,000: Department Head (turnaround 3 working days)
- INR 50,001 - 2,00,000: Finance Controller (turnaround 5 working days)
- Above INR 2,00,000: CFO and COO (turnaround 7 working days)
4.2 Travel policy:
- Domestic flights: economy class, booked at least 7 days in advance through the travel desk.
- Hotel cap: INR 6,500 per night in metro cities, INR 4,500 elsewhere.
- Daily allowance: INR 1,200 for domestic travel; actual meals reimbursed for client entertainment with approval.
- Personal vehicle use: reimbursed at INR 9 per km.
4.3 Vendor onboarding and procurement:
- New vendors submit GST certificate, PAN, bank proof and a signed NDA; Operations verifies and stores them as restricted documents.
- Purchases above INR 1,00,000 require three quotations or a documented single-source justification.
- Purchase orders are raised by Operations; invoices are paid within 30 days of approval.
- Vendor performance is reviewed every quarter on delivery time, quality and responsiveness.

5. IT Access and Asset Management
IT access follows the principle of least privilege: employees receive only the access needed for their role.
5.1 Standard access by role:
- All employees: Email, HRMS, intranet, knowledge base, ticketing (Approval for extra: Manager)
- HR: HRMS admin, payroll viewer, document repository HR folders (Approval for extra: HR Head)
- Operations: Procurement portal, vendor repository, document repository Ops folders (Approval for extra: Ops Head)
- Finance: ERP, payment gateway view, expense approval tool (Approval for extra: Finance Controller)
- Managers: Team dashboards, leave approval, budget reports (Approval for extra: Department Head)
5.2 Asset management:
- Every laptop and phone has an asset tag recorded in the asset register with the assigned employee.
- Lost or stolen devices must be reported to the IT Helpdesk within 24 hours; the device is remotely locked.
- Laptops are refreshed every 4 years; requests for early replacement need Department Head approval.
- Disk encryption and screen lock after 5 minutes are mandatory.
5.3 Security basics:
- Use multi-factor authentication for all company applications.
- Never share passwords; use the approved password manager.
- Confidential files must be stored only in the company document repository, not on personal drives.
- Report suspected phishing to security@northwind-ops.example.
5.4 IT service levels (SLA):
- Priority P1 (Email or HRMS outage): Response 30 minutes, Resolution 4 hours.
- Priority P2 (Laptop will not boot): Response 2 hours, Resolution 1 working day.
- Priority P3 (Software installation request): Response 1 working day, Resolution 3 working days.

6. Document Management Policy: Owner and ACL
Company knowledge is findable, trustworthy and protected. This policy defines how documents are uploaded to the company repository feeding the AI Office Operations Agent.
6.1 Supported formats:
- PDF (.pdf): Text extracted directly; scanned pages sent to OCR
- Word (.docx): Text, tables and headings extracted
- PowerPoint (.pptx): Slide text and speaker notes extracted
- Excel (.xlsx): Each sheet read as a table; formulas stored as values
- Images (.png, .jpg, .jpeg, .tiff): Always sent to OCR
6.2 Mandatory metadata before indexing:
- Owner: a named employee accountable for accuracy. Departed employees' documents must be reassigned within 7 days.
- Access control list (ACL): the users, groups or roles allowed to read the document. Default is 'owner only' until the uploader selects more.
- Classification: Public, Internal, Confidential or Restricted.
- Review date: when the content must be re-validated.
A document cannot be indexed, and therefore cannot be used by the AI agent, until it has an owner and an ACL. The agent only answers using documents that the asking user is permitted to see.
6.3 Upload statuses:
- Pending: Uploaded, waiting for owner/ACL or processing (Next action: Complete metadata)
- Processing: Parsing and OCR in progress (Next action: None)
- Indexed / Ready: Text searchable by the agent within ACL limits (Next action: None)
- Failed: Parsing or OCR failed; a reason is displayed (e.g. password-protected file, corrupt file, unsupported format, image too blurry)

7. Scanning and OCR Procedure
Many HR and Operations records still arrive on paper: signed contracts, ID proofs, delivery challans and vendor invoices. They must be scanned so that their text becomes searchable.
7.1 Scanning standards:
- Scan at 300 DPI minimum, in colour for signatures and stamps, grayscale for plain text.
- Keep pages upright and uncropped; remove staples and folds before scanning.
- One physical document equals one file. Name it DEPT_DocType_PartyName_YYYY-MM-DD, for example HR_OfferLetter_RahulNair_2026-09-14.pdf.
- Maximum upload size is 50 MB per file; split larger scans into parts.
7.2 Processing pipeline:
1. Upload -> 2. Detect -> 3. OCR (English and Malayalam supported) -> 4. Quality check -> 5. Index -> 6. Notify
7.3 Common failure reasons and fixes:
- Password protected: Remove password and re-upload
- Corrupt file: Re-scan or re-export the file
- Unsupported format: Convert to PDF or DOCX
- OCR confidence low: Re-scan at 300 DPI
- File too large: Over 50 MB; compress or split the file

8. Recurring Activities Calendar
8.1 Monthly activities:
- 1st working day: Attendance lock and leave balance update (Owner: HR Executive)
- 5th: Payroll inputs frozen (Owner: Payroll Lead)
- 7th: Salary credited (Owner: Finance)
- 10th: Expense claims for previous month closed (Owner: Finance)
- 15th: Vendor invoice payment run (Owner: Finance)
- Last Friday: Town hall and policy updates (Owner: HR Head)
- Last working day: Document repository review: missing owners or ACLs (Owner: Operations)
8.2 Quarterly and annual activities:
- Quarterly: Vendor performance review (Owner: Operations Head)
- Quarterly: Access rights recertification (Owner: IT Head)
- Quarterly: Fire drill and safety audit (Owner: Facilities)
- April: Annual appraisal cycle begins (Owner: HR Head)
- June: Statutory compliance audit (Owner: Legal)
- October: Policy handbook review and re-acknowledgement (Owner: HR Head)
- December: Business continuity test (Owner: Operations Head)
8.3 Upcoming dates (Q4 2026):
- 30 Oct 2026: Last day to submit September travel claims.
- 13 Nov 2026: Access rights recertification deadline.
- 27 Nov 2026: Town hall - new hybrid-work policy.
- 15 Dec 2026: Document owners confirm review dates for all Confidential files.

9. Frequently Asked Questions (FAQs)
Q. How do I apply for leave?
A. Raise a request in the HRMS under Leave > Apply. Your manager approves it within two working days.
Q. Can I upload a photo of a document taken on my phone?
A. Yes, JPG or PNG images are accepted and processed through OCR. Make sure the page is flat and well lit.
Q. Why is my uploaded document not searchable?
A. Check its status. If it is Pending, add an owner and ACL. If it is Failed, read the reason and re-upload.
Q. Who can see my document?
A. Only people or groups on its ACL. The AI agent applies the same rule when answering questions.
Q. What if the document owner leaves the company?
A. HR reassigns ownership to the manager or a named delegate within 7 days of the last working day.
Q. How long are records kept?
A. Employee records: 7 years after exit. Vendor contracts: 8 years after expiry. Expense receipts: 8 years.

10. Contacts, Escalation and Revision History
Team Contact Email Hours:
- HR Helpdesk: Priya Nair (hr@northwind-ops.example, 09:30-18:00)
- Operations Helpdesk: Arjun Das (ops@northwind-ops.example, 09:00-19:00)
- IT Helpdesk: Fatima Khan (it@northwind-ops.example, 24x7 for P1)
- Finance: Suresh Iyer (finance@northwind-ops.example, 09:30-18:00)
Escalation path: Helpdesk -> Department Head -> Head of HR & Operations (Anita Menon).
Revision History:
- Version 1.0 (03 Jan 2026): First release by A. Menon.
- Version 2.0 (01 Apr 2026): Added expense approval matrix and IT access table by A. Menon.
- Version 2.1 (01 Oct 2026): Added document owner/ACL policy and OCR procedure by A. Menon.
"""


def seed_company_documents(db: Session) -> Document | None:
    """Ensure the HR & Operations Knowledge Handbook exists as an indexed document."""
    existing = (
        db.query(Document)
        .filter(Document.original_filename == HANDBOOK_FILENAME)
        .first()
    )
    if existing is not None:
        # Story 7.3: the company handbook is for every employee, not only its owner.
        if not existing.shared_with_all_employees:
            existing.shared_with_all_employees = True
            db.commit()
        return existing

    # Find an owner employee (e.g. manager Maya Thompson or first employee)
    owner = (
        db.query(Employee)
        .filter(Employee.email.in_(["manager@example.com", "maya@northstar.example"]))
        .first()
    )
    if owner is None:
        owner = db.query(Employee).first()
    if owner is None:
        logger.warning("No employees available to own seeded company document.")
        return None

    # Write placeholder PDF bytes to storage so download works
    pdf_bytes = (
        b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources <<>> /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000015 00000 n \n0000000068 00000 n \n0000000125 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n212\n%%EOF\n"
    )
    try:
        write_document_file(HANDBOOK_STORED_NAME, pdf_bytes)
    except Exception as exc:
        logger.warning("Could not write seed handbook file to disk: %s", exc)

    doc = Document(
        id=str(uuid.uuid4()),
        original_filename=HANDBOOK_FILENAME,
        stored_filename=HANDBOOK_STORED_NAME,
        file_size=len(pdf_bytes),
        mime_type="application/pdf",
        owner_id=owner.id,
        status=DOC_STATUS_READY,
        shared_with_all_employees=True,
        extracted_text=HANDBOOK_TEXT,
        page_count=10,
        processing_completed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    logger.info("Seeded %s (id: %s) into company documents.", HANDBOOK_FILENAME, doc.id)
    return doc
