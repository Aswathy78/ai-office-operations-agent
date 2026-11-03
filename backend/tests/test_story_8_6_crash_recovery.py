"""Tests for Epic 8 Story 8.6: Workflow Crash Recovery & System Administrator Database Authentication."""

from datetime import datetime, timezone
import os
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.meeting.models import (
    Base,
    AuthAccount,
    Employee,
    SystemAdministrator,
    WorkflowDeadLetter,
    WorkflowEvent,
    WorkflowRun,
    WorkflowStep,
    UserNotification,
)
from app.auth_service import authenticate_password, provision_account
from app.workflow_service import (
    MAX_WORKFLOW_STEPS,
    PermanentWorkflowError,
    dead_letter_run,
    enqueue_workflow,
    get_workflow_steps,
    recover_crashed_workflows,
    recover_stale_workflows,
    record_event,
    workflow_cost_cap_usd,
)
from app.workflow_worker import _begin_step, _finish_step, _fail_step, process_workflow


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_system_administrator_database_and_separate_passwords(test_db: Session):
    """Must: Separate database record, email and passwords for Admin, Manager, and Employee."""
    # 1. Employee
    emp_employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        team="Engineering",
        is_active=True,
    )
    # 2. Manager
    emp_manager = Employee(
        id=str(uuid.uuid4()),
        name="Maya Thompson",
        email="manager@example.com",
        team="Engineering",
        is_active=True,
    )
    # 3. System Administrator
    emp_admin = Employee(
        id=str(uuid.uuid4()),
        name="System Administrator",
        email="admin@northstar.example",
        team="Operations",
        is_active=True,
    )
    test_db.add_all([emp_employee, emp_manager, emp_admin])
    test_db.commit()

    # Provision each with separate passwords
    acc_emp = provision_account(test_db, emp_employee.email, "EmployeeSecurePass123!", role="employee")
    acc_mgr = provision_account(test_db, emp_manager.email, "ManagerSecurePass123!", role="manager")
    acc_adm = provision_account(test_db, emp_admin.email, "AdminSecurePass123!", role="admin")

    # Verify separate accounts
    assert acc_emp.role == "employee"
    assert acc_mgr.role == "manager"
    assert acc_adm.role == "admin"
    assert acc_emp.id != acc_mgr.id != acc_adm.id

    # Verify dedicated system_administrators table entry
    admin_record = test_db.query(SystemAdministrator).filter(SystemAdministrator.email_normalized == "admin@northstar.example").first()
    assert admin_record is not None
    assert admin_record.role == "admin"
    assert admin_record.employee_id == emp_admin.id

    # Test authentication for each role
    auth_emp = authenticate_password(test_db, "ava.patel@northstar.example", "EmployeeSecurePass123!")
    assert auth_emp is not None
    assert auth_emp[0].role == "employee"

    auth_mgr = authenticate_password(test_db, "manager@example.com", "ManagerSecurePass123!")
    assert auth_mgr is not None
    assert auth_mgr[0].role == "manager"

    auth_adm = authenticate_password(test_db, "admin@northstar.example", "AdminSecurePass123!")
    assert auth_adm is not None
    assert auth_adm[0].role == "admin"

    # Cross password rejection
    assert authenticate_password(test_db, "admin@northstar.example", "ManagerSecurePass123!") is None
    assert authenticate_password(test_db, "manager@example.com", "AdminSecurePass123!") is None


def test_duplicate_delivery_causes_no_second_event(test_db: Session):
    """AC 1: Duplicate deliveries cause no second event."""
    run = enqueue_workflow(
        test_db,
        workflow_type="test_workflow",
        idempotency_key="unique-test-key-1",
        steps=("step_1", "step_2"),
    )

    # First event record
    ev1 = record_event(test_db, run.id, "step_completed", step_key="step_1", attempt_number=1)
    test_db.commit()

    # Duplicate delivery attempt for same event
    ev2 = record_event(test_db, run.id, "step_completed", step_key="step_1", attempt_number=1)
    test_db.commit()

    assert ev1.id == ev2.id

    all_events = test_db.query(WorkflowEvent).filter(
        WorkflowEvent.workflow_run_id == run.id,
        WorkflowEvent.event_type == "step_completed",
    ).all()
    assert len(all_events) == 1


def test_resume_from_last_checkpoint_after_worker_killed(test_db: Session):
    """AC 2: After a worker is killed, the run resumes from its last checkpoint."""
    run = enqueue_workflow(
        test_db,
        workflow_type="test_workflow",
        idempotency_key="unique-test-key-2",
        steps=("step_1", "step_2", "step_3"),
    )
    steps = get_workflow_steps(test_db, run.id)
    step1, step2, step3 = steps

    # Step 1 finished with a checkpoint before crash
    step1.status = "completed"
    step1.checkpoint = {"transcription_id": "tx-12345", "cost_usd": 0.50}
    test_db.commit()

    # Step 2 was in progress when worker crashed/killed
    now = datetime.now(timezone.utc)
    run.status = "processing"
    run.claimed_at = now
    step2.status = "processing"
    test_db.commit()

    # Crash recovery is triggered
    recovered_count = recover_crashed_workflows(test_db, max_lease_age_seconds=0)
    assert recovered_count == 1

    test_db.refresh(run)
    test_db.refresh(step1)
    test_db.refresh(step2)

    assert run.status == "retrying"
    assert step1.status == "completed"  # Step 1 preserved!
    assert step2.status == "retrying"   # Step 2 ready to retry!

    # Step 1's checkpoint is preserved and usable
    assert step1.checkpoint["transcription_id"] == "tx-12345"


def test_unrecoverable_runs_escalated_to_human(test_db: Session):
    """AC 3: Unrecoverable runs are escalated to a human (System Administrator)."""
    # Create system administrator employee and account
    admin_emp = Employee(
        id=str(uuid.uuid4()),
        name="System Administrator",
        email="admin@northstar.example",
        team="Operations",
        is_active=True,
    )
    test_db.add(admin_emp)
    test_db.commit()
    provision_account(test_db, admin_emp.email, "AdminSecurePass123!", role="admin")

    run = enqueue_workflow(
        test_db,
        workflow_type="test_workflow",
        idempotency_key="unique-test-key-3",
        steps=("step_1",),
        max_attempts=1,
    )
    steps = get_workflow_steps(test_db, run.id)
    step = steps[0]
    run.status = "processing"
    test_db.commit()

    # Permanent failure occurs
    perm_err = PermanentWorkflowError("Unrecoverable data corruption in source payload.")
    _fail_step(test_db, run, step, perm_err)

    test_db.refresh(run)
    assert run.status == "failed"

    # Verify dead-letter entry
    dlq = test_db.query(WorkflowDeadLetter).filter(WorkflowDeadLetter.workflow_run_id == run.id).first()
    assert dlq is not None
    assert dlq.status == "open"
    assert "Unrecoverable" in dlq.error_message

    # Verify notification escalated to System Administrator
    admin_notifications = (
        test_db.query(UserNotification)
        .filter(UserNotification.recipient_employee_id == admin_emp.id)
        .all()
    )
    assert len(admin_notifications) >= 1
    assert any("dead-letter" in n.summary for n in admin_notifications)


def test_step_limit_enforced_to_12_steps(test_db: Session):
    """AC 4: Runs are limited to 12 steps and cost cap."""
    run = enqueue_workflow(
        test_db,
        workflow_type="test_workflow",
        idempotency_key="unique-test-key-4",
        steps=("step_1",),
    )
    steps = get_workflow_steps(test_db, run.id)
    step = steps[0]

    # Pre-set steps_executed to 12
    run.payload = {"steps_executed": 12, "cost_usd": 1.0}
    run.status = "processing"
    test_db.commit()

    # Attempting 13th step must raise PermanentWorkflowError
    with pytest.raises(PermanentWorkflowError) as exc_info:
        _begin_step(test_db, run, step)

    assert "step limit of 12 steps exceeded" in str(exc_info.value)


def test_cost_cap_enforcement(test_db: Session, monkeypatch):
    """AC 4: Runs are limited to cost cap."""
    monkeypatch.setenv("WORKFLOW_COST_CAP_USD", "3.00")
    run = enqueue_workflow(
        test_db,
        workflow_type="test_workflow",
        idempotency_key="unique-test-key-5",
        steps=("step_1",),
    )
    steps = get_workflow_steps(test_db, run.id)
    step = steps[0]

    # Pre-set cost to $3.50 (exceeds cap of $3.00)
    run.payload = {"steps_executed": 2, "cost_usd": 3.50}
    run.status = "processing"
    test_db.commit()

    with pytest.raises(PermanentWorkflowError) as exc_info:
        _begin_step(test_db, run, step)

    assert "cost cap of $3.00 exceeded" in str(exc_info.value)

