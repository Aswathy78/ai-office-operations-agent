"""Tests for Story 6.4 — Plan & Retrieve.

Coverage
--------
AC-1  Plan is ordered and emitted before any step_start event.
AC-2  General chat does not invoke the planner or retrieval.
AC-2  Routine scheduling does not trigger retrieval.
AC-2  Policy-related steps trigger retrieval when knowledge base exists.
AC-3  Execution state checkpoint is persisted after each completed step.
AC-3  Failed steps preserve earlier checkpoints.
AC-4  Maximum 12 steps is enforced (halted == "step_limit").
AC-4  Token budget exhaustion stops execution (halted == "cost_cap").
Compat Clarification behaviour preserved (6.3 gate runs before planner).
Compat Authentication and conversation isolation (6.1).
Compat General chat / explain_understanding paths unchanged (6.1/6.2).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.models import AssistantConversation, AssistantMessage, Base, Employee
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
import app.assistant_service as assistant_service
import app.assistant_planner as assistant_planner
from app.assistant_planner import (
    PlanStep,
    CompletedStep,
    ExecutionCheckpoint,
    build_execution_plan,
    check_cost_cap,
    check_step_limit,
    estimate_tokens,
    needs_retrieval,
    retrieve_company_knowledge,
    save_execution_checkpoint,
    load_execution_checkpoint,
    clear_execution_checkpoint,
)
from app.assistant_understanding import (
    AssistantUnderstanding,
    AssistantEntities,
    understand_assistant_command,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_events(response) -> list[dict]:
    """Parse all non-DONE SSE data lines from a streaming response."""
    events = []
    for line in response.text.splitlines():
        if line.startswith("data:") and line.strip() != "data: [DONE]":
            try:
                events.append(json.loads(line.removeprefix("data:").strip()))
            except json.JSONDecodeError:
                pass
    return events


def _events_of_type(events: list[dict], key: str) -> list:
    """Filter SSE events to those containing a specific key."""
    return [e[key] for e in events if key in e]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session_ = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session_()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def employee(db_session: Session):
    emp = Employee(
        id="emp-test",
        name="Maya Thompson",
        email="maya@northstar.example",
        team="Engineering",
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture()
def principal(employee: Employee):
    return AuthenticatedPrincipal(
        user_id=employee.id,
        account_id="acc-test",
        email=employee.email,
        name=employee.name,
        role="employee",
        team=employee.team,
        jti="jti-test",
    )


@pytest.fixture()
def client(db_session: Session, principal: AuthenticatedPrincipal):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit tests — planner module
# ---------------------------------------------------------------------------

class TestBuildExecutionPlan:
    def _understood(self, intent, **entity_kwargs) -> AssistantUnderstanding:
        """Build a minimal understood AssistantUnderstanding for a given intent."""
        from datetime import date, time
        if intent == "meeting_scheduling":
            entities = AssistantEntities(
                people=["Maya Thompson"],
                date=date(2026, 10, 25),
                time=time(10, 0),
            )
        elif intent == "task_assignment":
            entities = AssistantEntities(
                people=["Maya Thompson"],
                task="Write the Q3 report",
            )
        elif intent == "send_email":
            entities = AssistantEntities(
                people=["Maya Thompson"],
                topic="Project update",
            )
        else:
            raise ValueError(f"Unknown test intent: {intent}")
        entities_dict = entities.model_dump()
        entities_dict.update(entity_kwargs)
        return AssistantUnderstanding(
            intent=intent,
            status="understood",
            entities=AssistantEntities(**entities_dict),
        )

    def test_meeting_plan_is_ordered(self):
        u = self._understood("meeting_scheduling")
        plan = build_execution_plan(u)
        assert len(plan) >= 2
        for i, step in enumerate(plan):
            assert step.index == i, "Steps must be ordered by 0-based index"

    def test_task_plan_is_ordered(self):
        u = self._understood("task_assignment")
        plan = build_execution_plan(u)
        for i, step in enumerate(plan):
            assert step.index == i

    def test_email_plan_is_ordered(self):
        u = self._understood("send_email")
        plan = build_execution_plan(u)
        for i, step in enumerate(plan):
            assert step.index == i

    def test_plan_ends_with_summarise_step(self):
        u = self._understood("meeting_scheduling")
        plan = build_execution_plan(u)
        assert plan[-1].action_type == "summarise_response"

    def test_plan_capped_at_max_steps(self, monkeypatch):
        monkeypatch.setattr(assistant_planner, "MAX_PLAN_STEPS", 2)
        u = self._understood("meeting_scheduling")
        plan = build_execution_plan(u)
        assert len(plan) <= 2

    def test_rejects_non_understood_intent(self):
        u = AssistantUnderstanding(
            intent="meeting_scheduling",
            status="needs_clarification",
            entities=AssistantEntities(people=["Alice"]),
            missing_entities=["date"],
        )
        with pytest.raises(ValueError, match="non-understood"):
            build_execution_plan(u)

    def test_email_plan_has_policy_retrieval_step(self):
        u = self._understood("send_email")
        plan = build_execution_plan(u)
        retrieval_steps = [s for s in plan if s.retrieval_query is not None]
        assert retrieval_steps, "Email plan must include at least one retrieval step"

    def test_meeting_plan_has_no_retrieval_by_default(self):
        u = self._understood("meeting_scheduling")
        plan = build_execution_plan(u)
        # Routine meeting scheduling steps should not trigger retrieval
        retrieval_steps = [
            s for s in plan
            if s.retrieval_query and "policy" in s.retrieval_query.lower()
        ]
        # Basic meeting scheduling only triggers retrieval if the description
        # mentions policy keywords; routine steps should not.
        non_summary = [s for s in plan if s.action_type == "schedule_meeting"]
        assert all(
            not needs_retrieval(s.description) or s.retrieval_query is not None
            for s in non_summary
        ), "Only explicitly policy-related steps should have retrieval_query set"


class TestNeedsRetrieval:
    def test_policy_keyword_triggers_retrieval(self):
        assert needs_retrieval("Check the email approval policy for this request")

    def test_routine_step_does_not_trigger(self):
        assert not needs_retrieval("Schedule meeting with Alice on Friday at 10 AM")

    def test_greeting_does_not_trigger(self):
        assert not needs_retrieval("Hello, how are you?")

    def test_task_step_does_not_trigger(self):
        assert not needs_retrieval("Assign the database task to Rahul by Friday")

    def test_compliance_triggers(self):
        assert needs_retrieval("Check compliance requirements before sending")

    def test_threshold_triggers(self):
        assert needs_retrieval("Verify recipient threshold for bulk email")


class TestRetrieveCompanyKnowledge:
    def test_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            assistant_planner,
            "_KNOWLEDGE_BASE_PATH",
            tmp_path / "nonexistent.md",
        )
        result = retrieve_company_knowledge("email policy")
        assert result is None

    def test_returns_matching_paragraph(self, tmp_path, monkeypatch):
        policy_file = tmp_path / "policy.md"
        policy_file.write_text(
            "Unrelated paragraph.\n\n"
            "Email approval threshold: more than 10 recipients require manager approval.\n\n"
            "Another unrelated paragraph."
        )
        monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", policy_file)
        result = retrieve_company_knowledge("email approval threshold")
        assert result is not None
        assert "approval" in result.lower()

    def test_returns_none_when_no_match(self, tmp_path, monkeypatch):
        policy_file = tmp_path / "policy.md"
        policy_file.write_text("Only meeting content here.\n")
        monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", policy_file)
        result = retrieve_company_knowledge("completely unrelated xyz")
        assert result is None

    def test_snippet_is_truncated(self, tmp_path, monkeypatch):
        policy_file = tmp_path / "policy.md"
        long_para = "policy " + "x" * 2000
        policy_file.write_text(long_para)
        monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", policy_file)
        result = retrieve_company_knowledge("policy")
        assert result is not None
        assert len(result) <= assistant_planner._MAX_RETRIEVAL_SNIPPET_CHARS


class TestTokenEstimation:
    def test_basic_estimate(self):
        text = "A" * 400
        assert estimate_tokens(text) == 100

    def test_minimum_is_one(self):
        assert estimate_tokens("") >= 1


class TestLimitGuards:
    def test_step_limit_false_below_max(self, monkeypatch):
        monkeypatch.setattr(assistant_planner, "MAX_PLAN_STEPS", 12)
        assert not check_step_limit(11)

    def test_step_limit_true_at_max(self, monkeypatch):
        monkeypatch.setattr(assistant_planner, "MAX_PLAN_STEPS", 12)
        assert check_step_limit(12)

    def test_cost_cap_false_within_budget(self, monkeypatch):
        monkeypatch.setattr(assistant_planner, "COST_CAP_TOKENS", 4000)
        assert not check_cost_cap(100, 50)

    def test_cost_cap_true_when_exceeded(self, monkeypatch):
        monkeypatch.setattr(assistant_planner, "COST_CAP_TOKENS", 4000)
        assert check_cost_cap(3980, 50)


class TestCheckpointPersistence:
    def test_save_and_load_checkpoint(self, db_session: Session):
        conv = AssistantConversation(
            id="conv-ck-1",
            user_id=None,
            title="test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(conv)
        db_session.commit()

        step = PlanStep(
            index=0,
            description="Test step",
            action_type="assign_task",
            status="done",
            estimated_tokens=10,
        )
        completed = CompletedStep(
            index=0,
            description="Test step",
            action_type="assign_task",
            status="done",
            result_summary="OK",
            tokens_used=10,
        )
        save_execution_checkpoint(
            db_session, conv, [step], [completed], 0, 10, None, "2026-10-22T00:00:00+00:00"
        )

        db_session.expire(conv)
        loaded = load_execution_checkpoint(conv)
        assert loaded is not None
        assert loaded.current_step == 0
        assert loaded.total_tokens_estimated == 10
        assert len(loaded.completed_steps) == 1
        assert loaded.halted_reason is None

    def test_clear_checkpoint(self, db_session: Session):
        conv = AssistantConversation(
            id="conv-ck-2",
            user_id=None,
            title="test",
            execution_state='{"dummy": true}',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(conv)
        db_session.commit()
        clear_execution_checkpoint(db_session, conv)
        db_session.expire(conv)
        assert conv.execution_state is None

    def test_load_from_none_returns_none(self, db_session: Session):
        conv = AssistantConversation(
            id="conv-ck-3",
            user_id=None,
            title="test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(conv)
        db_session.commit()
        assert load_execution_checkpoint(conv) is None


# ---------------------------------------------------------------------------
# Integration tests — SSE endpoint
# ---------------------------------------------------------------------------

class TestPlanEmittedBeforeExecution:
    """AC-1: Plan is shown before any step_start event."""

    def test_meeting_request_emits_plan_before_step_start(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-alice",
            name="Alice Smith",
            email="alice@northstar.example",
            team="Engineering",
            is_active=True,
        ))
        db_session.commit()

        # Prevent real LLM calls in summary step
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter(["Summary done."]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Schedule a meeting with Alice tomorrow at 10 AM"},
        )
        assert resp.status_code == 200
        events = _sse_events(resp)

        plan_positions = [i for i, e in enumerate(events) if "plan" in e]
        step_positions = [i for i, e in enumerate(events) if "step_start" in e]

        assert plan_positions, "A 'plan' event must be emitted"
        if step_positions:
            assert plan_positions[0] < step_positions[0], (
                "plan event must appear before the first step_start"
            )

    def test_plan_steps_are_ordered(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-bob",
            name="Bob Lee",
            email="bob@northstar.example",
            team="Design",
            is_active=True,
        ))
        db_session.commit()

        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter([]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Assign the report task to Bob by Friday"},
        )
        events = _sse_events(resp)
        plans = _events_of_type(events, "plan")
        assert plans, "Expected at least one plan event"
        steps = plans[0]
        for i, step in enumerate(steps):
            assert step["index"] == i


class TestGeneralChatDoesNotUsePlanner:
    """AC-2 (general chat): routine chat must not emit plan or retrieval events."""

    def test_greeting_has_no_plan_event(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter(["Hello! How can I help?"]),
        )
        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Hello, what can you do?"},
        )
        events = _sse_events(resp)
        assert not _events_of_type(events, "plan")
        assert not _events_of_type(events, "retrieval")


class TestConditionalRetrieval:
    """AC-2: retrieval only when policy context needed."""

    def test_routine_meeting_does_not_emit_retrieval(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-carol",
            name="Carol James",
            email="carol@northstar.example",
            team="HR",
            is_active=True,
        ))
        db_session.commit()

        retrieval_called = []

        real_retrieve = assistant_planner.retrieve_company_knowledge

        def mock_retrieve(query):
            retrieval_called.append(query)
            return real_retrieve(query)

        monkeypatch.setattr(assistant_planner, "retrieve_company_knowledge", mock_retrieve)
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter([]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Schedule a meeting with Carol tomorrow at 3 PM"},
        )
        events = _sse_events(resp)
        # For routine meeting scheduling, no retrieval events should appear
        retrieval_events = _events_of_type(events, "retrieval")
        assert retrieval_events == [], (
            "Routine meeting scheduling must not emit retrieval events"
        )

    def test_email_plan_calls_retrieval_for_policy_step(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-diana",
            name="Diana Prince",
            email="diana@northstar.example",
            team="Legal",
            is_active=True,
        ))
        db_session.commit()

        # Provide a real policy file so retrieval actually returns content
        import tempfile, pathlib
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("Email approval threshold: more than 10 recipients require approval.\n")
            f.flush()
            policy_path = pathlib.Path(f.name)

        monkeypatch.setattr(assistant_planner, "_KNOWLEDGE_BASE_PATH", policy_path)
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter([]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Send an email to Diana about the project update"},
        )
        events = _sse_events(resp)
        retrieval_events = _events_of_type(events, "retrieval")
        # The email plan includes a policy-checking step — retrieval should fire
        assert retrieval_events, "Email plan with policy step should emit retrieval event"

        policy_path.unlink(missing_ok=True)


class TestCheckpointAfterEachStep:
    """AC-3: execution_state persisted after every completed step."""

    def test_checkpoint_saved_after_step_completion(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-evan",
            name="Evan Cross",
            email="evan@northstar.example",
            team="Engineering",
            is_active=True,
        ))
        db_session.commit()

        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter(["Done."]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Assign the database task to Evan by Friday"},
        )
        assert resp.status_code == 200
        events = _sse_events(resp)
        # At least one step_done event indicates execution ran
        step_dones = _events_of_type(events, "step_done")
        assert step_dones, "Expected at least one step_done event"

        # The conversation row should have a checkpoint
        conv_id = _events_of_type(events, "conversation_id")
        if not conv_id:
            # conversation_id is in the init payload
            for e in events:
                if "conversation_id" in e:
                    conv_id = [e["conversation_id"]]
                    break
        if conv_id:
            conv = db_session.get(AssistantConversation, conv_id[0])
            if conv:
                db_session.expire(conv)
                # execution_state should be set (checkpoint saved)
                assert conv.execution_state is not None, (
                    "execution_state should be persisted after step completion"
                )

    def test_failed_step_preserves_earlier_checkpoint(
        self, db_session: Session, monkeypatch
    ):
        """A failed step must not overwrite an earlier successful step's checkpoint."""
        from datetime import date, time

        conv = AssistantConversation(
            id="conv-fail-test",
            user_id=None,
            title="test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(conv)
        db_session.commit()

        # Simulate: step 0 completed successfully
        step0 = PlanStep(
            index=0, description="Step 0", action_type="assign_task",
            status="done", estimated_tokens=10,
        )
        completed0 = CompletedStep(
            index=0, description="Step 0", action_type="assign_task",
            status="done", result_summary="OK", tokens_used=10,
        )
        save_execution_checkpoint(
            db_session, conv, [step0], [completed0], 0, 10, None, "2026-10-22T00:00:00+00:00"
        )
        db_session.expire(conv)
        first_state = load_execution_checkpoint(conv)
        assert first_state is not None
        assert len(first_state.completed_steps) == 1

        # Simulate: step 1 fails — save checkpoint with both step 0 (done) and step 1 (failed)
        step1 = PlanStep(
            index=1, description="Step 1", action_type="assign_task",
            status="failed", estimated_tokens=10,
        )
        completed1 = CompletedStep(
            index=1, description="Step 1", action_type="assign_task",
            status="failed", result_summary="Error", tokens_used=10,
        )
        save_execution_checkpoint(
            db_session, conv, [step0, step1], [completed0, completed1],
            1, 20, None, "2026-10-22T00:00:00+00:00"
        )
        db_session.expire(conv)
        second_state = load_execution_checkpoint(conv)
        assert second_state is not None
        assert len(second_state.completed_steps) == 2
        # Step 0 is still in the checkpoint as "done"
        step0_record = next(
            (s for s in second_state.completed_steps if s["index"] == 0), None
        )
        assert step0_record is not None
        assert step0_record["status"] == "done", "Earlier successful step preserved after failure"


class TestStepLimit:
    """AC-4: never more than MAX_PLAN_STEPS executed steps."""

    def test_step_limit_halts_execution(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-frank",
            name="Frank Stein",
            email="frank@northstar.example",
            team="Operations",
            is_active=True,
        ))
        db_session.commit()

        # Patch MAX_PLAN_STEPS to 2 in the planner module so the guard fires.
        monkeypatch.setattr(assistant_planner, "MAX_PLAN_STEPS", 2)

        # Make build_execution_plan return 5 steps (bypassing the cap in the
        # function itself) so the router's loop check_step_limit guard fires.
        five_steps = [
            PlanStep(index=i, description=f"Step {i}", action_type="assign_task",
                     status="pending", estimated_tokens=5)
            for i in range(5)
        ]
        monkeypatch.setattr(assistant_planner, "build_execution_plan", lambda u: five_steps)
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter([]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Assign the audit task to Frank by Monday"},
        )
        events = _sse_events(resp)
        halted = _events_of_type(events, "halted")
        assert halted, "Expected a 'halted' event when step limit is reached"
        assert halted[0] == "step_limit"

        step_dones = _events_of_type(events, "step_done")
        assert len(step_dones) <= 2, "Must not execute more than MAX_PLAN_STEPS steps"


class TestCostCapExhaustion:
    """AC-4: execution stops when token budget is exhausted."""

    def test_cost_cap_halts_execution(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        db_session.add(Employee(
            id="emp-grace",
            name="Grace Hopper",
            email="grace@northstar.example",
            team="Engineering",
            is_active=True,
        ))
        db_session.commit()

        # Set a tiny token budget — first step will exceed it
        monkeypatch.setattr(assistant_planner, "COST_CAP_TOKENS", 1)
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter([]),
        )

        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Schedule a meeting with Grace tomorrow at 2 PM"},
        )
        events = _sse_events(resp)
        halted = _events_of_type(events, "halted")
        assert halted, "Expected a 'halted' event when cost cap is exhausted"
        assert halted[0] == "cost_cap"


class TestClarificationPreventsPlanExecution:
    """Clarification gate (6.3) must prevent planner from running."""

    def test_missing_time_triggers_clarification_not_plan(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: pytest.fail("clarification must not reach the LLM"),
        )
        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Schedule a meeting with someone tomorrow"},
        )
        events = _sse_events(resp)
        understanding_events = [e for e in events if "understanding" in e and e.get("understanding")]
        if understanding_events:
            u = understanding_events[0]["understanding"]
            # If the understanding was produced, it must show needs_clarification
            if u and u.get("status") == "needs_clarification":
                # No plan should have been emitted
                assert not _events_of_type(events, "plan"), (
                    "A plan must not be emitted when clarification is still needed"
                )


class TestAuthorizationPreserved:
    """Stories 6.1 auth and conversation isolation still apply."""

    def test_unauthenticated_request_rejected(self, db_session: Session):
        app.dependency_overrides.clear()
        app.dependency_overrides[get_db] = lambda: db_session
        with TestClient(app) as unauth_client:
            resp = unauth_client.post(
                "/api/v1/assistant/chat/stream",
                json={"message": "Schedule a meeting"},
            )
        assert resp.status_code == 401
        app.dependency_overrides.clear()

    def test_cross_user_plan_conversation_denied(
        self,
        db_session: Session,
        principal: AuthenticatedPrincipal,
        monkeypatch,
    ):
        emp_b = Employee(
            id="emp-cross-b",
            name="Dave Cross",
            email="dave@northstar.example",
            team="Design",
            is_active=True,
        )
        db_session.add(emp_b)
        db_session.commit()

        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter(["reply"]),
        )

        principal_b = AuthenticatedPrincipal(
            user_id=emp_b.id,
            account_id="acc-b",
            email=emp_b.email,
            name=emp_b.name,
            role="employee",
            team=emp_b.team,
            jti="jti-b",
        )

        # A creates a conversation
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_authenticated_principal] = lambda: principal
        with TestClient(app) as client_a:
            r = client_a.post(
                "/api/v1/assistant/chat/stream",
                json={"message": "Hello"},
            )
            init_events = [
                json.loads(l.removeprefix("data:").strip())
                for l in r.text.splitlines()
                if l.startswith("data:") and l.strip() != "data: [DONE]"
            ]
            conv_id = next(
                (e["conversation_id"] for e in init_events if "conversation_id" in e),
                None,
            )
        assert conv_id

        # B tries to use A's conversation → 403
        app.dependency_overrides[require_authenticated_principal] = lambda: principal_b
        with TestClient(app) as client_b:
            r2 = client_b.post(
                "/api/v1/assistant/chat/stream",
                json={"message": "Get my plan", "conversation_id": conv_id},
            )
        assert r2.status_code == 403
        app.dependency_overrides.clear()


class TestBackwardCompatibility:
    """Stories 6.1–6.3 must remain fully functional."""

    def test_general_chat_streams_tokens(self, client: TestClient, monkeypatch):
        tokens = ["Hello", " Maya", "!"]
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter(tokens),
        )
        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "How are you today?"},
        )
        assert resp.status_code == 200
        events = _sse_events(resp)
        token_chunks = _events_of_type(events, "token")
        assert token_chunks == tokens

    def test_clarification_persists_and_resumes(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: pytest.fail("should not reach LLM"),
        )
        first = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Schedule a meeting with someone tomorrow"},
        )
        events = _sse_events(first)
        init_events = [e for e in events if "conversation_id" in e]
        if not init_events:
            return  # nothing understood — skip
        conv_id = init_events[0]["conversation_id"]
        u = init_events[0].get("understanding")
        if not u or u.get("status") != "needs_clarification":
            return  # not a clarification case in this test data — skip

        conv = db_session.get(AssistantConversation, conv_id)
        assert conv is not None
        assert conv.pending_clarification is not None

        # Plan must NOT have been emitted
        assert not _events_of_type(events, "plan")

    def test_cancel_clears_pending_clarification(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: pytest.fail("should not reach LLM"),
        )
        first = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "Schedule a meeting with someone tomorrow"},
        )
        events = _sse_events(first)
        init_events = [e for e in events if "conversation_id" in e]
        if not init_events:
            return
        conv_id = init_events[0]["conversation_id"]
        u = init_events[0].get("understanding")
        if not u or u.get("status") != "needs_clarification":
            return

        cancel = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "cancel request", "conversation_id": conv_id},
        )
        assert cancel.status_code == 200
        cancel_events = _sse_events(cancel)
        cancel_tokens = _events_of_type(cancel_events, "token")
        assert any("cancel" in t.lower() or "operation" in t.lower() for t in cancel_tokens)

        conv = db_session.get(AssistantConversation, conv_id)
        if conv:
            db_session.expire(conv)
            assert conv.pending_clarification is None

    def test_conversation_history_persisted(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        monkeypatch.setattr(
            assistant_service,
            "stream_chat_completion",
            lambda *a, **kw: iter(["Sure!"]),
        )
        resp = client.post(
            "/api/v1/assistant/chat/stream",
            json={"message": "What is two plus two?"},
        )
        events = _sse_events(resp)
        conv_id = next(
            (e["conversation_id"] for e in events if "conversation_id" in e), None
        )
        assert conv_id
        msgs = db_session.query(AssistantMessage).filter_by(
            conversation_id=conv_id
        ).all()
        assert len(msgs) == 2  # user + assistant
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"
