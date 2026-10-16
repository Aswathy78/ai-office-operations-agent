"""Tests for FR-3 Step 5: External Guest Lobby and Organizer Admission/Rejection.

Covers:
- POST /{meeting_id}/lobby       - guest enters lobby
- GET  /{meeting_id}/lobby       - organizer lists pending guests
- POST /{meeting_id}/lobby/{id}/decision  - organizer admits/rejects guest
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from main import app
from app.db.database import get_db
from app.meeting.models import Base, Meeting, MeetingAttendanceLog


# Test fixtures

TEST_DB_URL = "sqlite:///./test_lobby.db"


@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    TestingSessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _future_meeting(client):
    now = datetime.now(timezone.utc)
    payload = {
        "title": "Lobby Test Meeting",
        "start_at": (now + timedelta(minutes=5)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    }
    response = client.post("/api/v1/meetings/", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _expired_meeting(client):
    now = datetime.now(timezone.utc)
    payload = {
        "title": "Expired Lobby Meeting",
        "start_at": (now - timedelta(hours=2)).isoformat(),
        "end_at": (now - timedelta(hours=1)).isoformat(),
    }
    response = client.post("/api/v1/meetings/", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# POST /{meeting_id}/lobby

class TestGuestJoinLobby:
    def test_guest_joins_lobby_returns_202(self, client):
        meeting = _future_meeting(client)
        payload = {"guest_name": "Alice Guest", "guest_email": "alice@external.com"}
        resp = client.post(f"/api/v1/meetings/{meeting['id']}/lobby", json=payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["guest_name"] == "Alice Guest"
        assert body["guest_email"] == "alice@external.com"
        assert body["status"] == "pending"
        assert body["meeting_id"] == meeting["id"]
        assert body["decided_at"] is None
        assert body["attendance_log_id"] is None

    def test_guest_joins_lobby_without_email(self, client):
        meeting = _future_meeting(client)
        payload = {"guest_name": "Bob NoEmail"}
        resp = client.post(f"/api/v1/meetings/{meeting['id']}/lobby", json=payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["guest_email"] is None
        assert body["status"] == "pending"

    def test_guest_join_lobby_idempotent_by_email(self, client):
        meeting = _future_meeting(client)
        payload = {"guest_name": "Carol", "guest_email": "carol@ext.com"}
        r1 = client.post(f"/api/v1/meetings/{meeting['id']}/lobby", json=payload)
        r2 = client.post(f"/api/v1/meetings/{meeting['id']}/lobby", json=payload)
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["id"] == r2.json()["id"]

    def test_guest_join_lobby_idempotent_by_name_only(self, client):
        meeting = _future_meeting(client)
        payload = {"guest_name": "Dan NoEmail"}
        r1 = client.post(f"/api/v1/meetings/{meeting['id']}/lobby", json=payload)
        r2 = client.post(f"/api/v1/meetings/{meeting['id']}/lobby", json=payload)
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["id"] == r2.json()["id"]

    def test_guest_join_nonexistent_meeting_returns_404(self, client):
        resp = client.post(
            "/api/v1/meetings/00000000-dead-beef-0000-000000000000/lobby",
            json={"guest_name": "Ghost"},
        )
        assert resp.status_code == 404

    def test_guest_join_expired_meeting_returns_410(self, client):
        meeting = _expired_meeting(client)
        resp = client.post(
            f"/api/v1/meetings/{meeting['id']}/lobby",
            json={"guest_name": "Late Guest"},
        )
        assert resp.status_code == 410

    def test_guest_name_cannot_be_empty(self, client):
        meeting = _future_meeting(client)
        resp = client.post(
            f"/api/v1/meetings/{meeting['id']}/lobby",
            json={"guest_name": ""},
        )
        assert resp.status_code == 422


# GET /{meeting_id}/lobby

class TestListPendingGuests:
    def test_list_returns_empty_when_no_requests(self, client):
        meeting = _future_meeting(client)
        resp = client.get(f"/api/v1/meetings/{meeting['id']}/lobby")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_only_pending_requests(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Eve"})
        client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Frank"})
        resp = client.get(f"/api/v1/meetings/{mid}/lobby")
        assert resp.status_code == 200
        names = {g["guest_name"] for g in resp.json()}
        assert "Eve" in names
        assert "Frank" in names

    def test_list_excludes_decided_requests(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        r = client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Gina"})
        request_id = r.json()["id"]
        client.post(
            f"/api/v1/meetings/{mid}/lobby/{request_id}/decision",
            json={"action": "reject"},
        )
        resp = client.get(f"/api/v1/meetings/{mid}/lobby")
        assert resp.status_code == 200
        names = [g["guest_name"] for g in resp.json()]
        assert "Gina" not in names


# POST /{meeting_id}/lobby/{id}/decision

class TestOrganizerDecision:
    def test_admit_guest_sets_status_admitted(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        r = client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Hank"})
        rid = r.json()["id"]
        resp = client.post(
            f"/api/v1/meetings/{mid}/lobby/{rid}/decision",
            json={"action": "admit"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "admitted"
        assert body["decided_at"] is not None
        assert body["attendance_log_id"] is None

    def test_reject_guest_sets_status_rejected(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        r = client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Iris"})
        rid = r.json()["id"]
        resp = client.post(
            f"/api/v1/meetings/{mid}/lobby/{rid}/decision",
            json={"action": "reject"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["decided_at"] is not None
        assert body["attendance_log_id"] is None

    def test_double_decision_returns_409(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        r = client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Jack"})
        rid = r.json()["id"]
        client.post(f"/api/v1/meetings/{mid}/lobby/{rid}/decision", json={"action": "admit"})
        resp2 = client.post(
            f"/api/v1/meetings/{mid}/lobby/{rid}/decision",
            json={"action": "reject"},
        )
        assert resp2.status_code == 409

    def test_decision_for_wrong_meeting_returns_404(self, client):
        meeting_a = _future_meeting(client)
        meeting_b = _future_meeting(client)
        r = client.post(
            f"/api/v1/meetings/{meeting_a['id']}/lobby",
            json={"guest_name": "Kate"},
        )
        rid = r.json()["id"]
        resp = client.post(
            f"/api/v1/meetings/{meeting_b['id']}/lobby/{rid}/decision",
            json={"action": "admit"},
        )
        assert resp.status_code == 404

    def test_decision_for_nonexistent_request_returns_404(self, client):
        meeting = _future_meeting(client)
        resp = client.post(
            f"/api/v1/meetings/{meeting['id']}/lobby/00000000-dead-0000-0000-000000000000/decision",
            json={"action": "admit"},
        )
        assert resp.status_code == 404

    def test_invalid_action_returns_422(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        r = client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Liam"})
        rid = r.json()["id"]
        resp = client.post(
            f"/api/v1/meetings/{mid}/lobby/{rid}/decision",
            json={"action": "maybe"},
        )
        assert resp.status_code == 422

    def test_admitted_guest_removed_from_pending_list(self, client):
        meeting = _future_meeting(client)
        mid = meeting["id"]
        r = client.post(f"/api/v1/meetings/{mid}/lobby", json={"guest_name": "Mia"})
        rid = r.json()["id"]
        before = client.get(f"/api/v1/meetings/{mid}/lobby")
        names_before = [g["guest_name"] for g in before.json()]
        assert "Mia" in names_before
        client.post(f"/api/v1/meetings/{mid}/lobby/{rid}/decision", json={"action": "admit"})
        after = client.get(f"/api/v1/meetings/{mid}/lobby")
        names_after = [g["guest_name"] for g in after.json()]
        assert "Mia" not in names_after

    def test_admission_notification_reaches_connected_guest(self, client):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Nora"},
        ).json()

        with client.websocket_connect(
            f"/api/v1/meetings/{meeting_id}/lobby-ws?guest_request_id={request['id']}"
        ) as websocket:
            welcome = websocket.receive_json()
            assert welcome["type"] == "lobby_welcome"

            response = client.post(
                f"/api/v1/meetings/{meeting_id}/lobby/{request['id']}/decision",
                json={"decision": "admit"},
            )
            assert response.status_code == 200
            event = websocket.receive_json()

        assert event["type"] == "guest_admitted"
        assert event["guest_request_id"] == request["id"]
        assert event["meeting_id"] == meeting_id

    def test_rejection_notification_reaches_connected_guest(self, client):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Omar"},
        ).json()

        with client.websocket_connect(
            f"/api/v1/meetings/{meeting_id}/lobby-ws?guest_request_id={request['id']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "lobby_welcome"
            response = client.post(
                f"/api/v1/meetings/{meeting_id}/lobby/{request['id']}/decision",
                json={"decision": "reject"},
            )
            assert response.status_code == 200
            event = websocket.receive_json()

        assert event["type"] == "guest_rejected"
        assert event["guest_request_id"] == request["id"]

    def test_lobby_websocket_rejects_missing_or_unknown_request(self, client):
        meeting = _future_meeting(client)
        paths = [
            f"/api/v1/meetings/{meeting['id']}/lobby-ws",
            f"/api/v1/meetings/{meeting['id']}/lobby-ws?guest_request_id=unknown",
        ]
        for path in paths:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(path):
                    pass
            assert exc_info.value.code == 1008

    def test_lobby_websocket_rejects_request_from_another_meeting(self, client):
        meeting_a = _future_meeting(client)
        meeting_b = _future_meeting(client)
        request = client.post(
            f"/api/v1/meetings/{meeting_a['id']}/lobby",
            json={"guest_name": "Pia"},
        ).json()

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/meetings/{meeting_b['id']}/lobby-ws"
                f"?guest_request_id={request['id']}"
            ):
                pass
        assert exc_info.value.code == 1008

    def test_lobby_websocket_allows_reconnection_for_active_guest(self, client):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Tariq"},
        ).json()

        url = f"/api/v1/meetings/{meeting_id}/lobby-ws?guest_request_id={request['id']}"

        with client.websocket_connect(url) as ws1:
            welcome1 = ws1.receive_json()
            assert welcome1["type"] == "lobby_welcome"

            # Reconnecting with the same guest_request_id should succeed and take over
            with client.websocket_connect(url) as ws2:
                welcome2 = ws2.receive_json()
                assert welcome2["type"] == "lobby_welcome"

                # Previous connection should have been closed cleanly
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws1.receive_json()
                assert exc_info.value.code == 1000

                # Organizer decision reaches the active reconnected socket
                client.post(
                    f"/api/v1/meetings/{meeting_id}/lobby/{request['id']}/decision",
                    json={"decision": "admit"},
                )
                event = ws2.receive_json()
                assert event["type"] == "guest_admitted"
                assert event["guest_request_id"] == request["id"]

    def test_lobby_websocket_rejects_expired_meeting(self, client, db_session):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Late Guest"},
        ).json()

        # Age the meeting past its end time
        db_session.query(Meeting).filter_by(id=meeting_id).update({
            Meeting.end_at: datetime.now(timezone.utc) - timedelta(hours=1)
        })
        db_session.commit()

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/meetings/{meeting_id}/lobby-ws?guest_request_id={request['id']}"
            ):
                pass
        assert exc_info.value.code == 1008

    @pytest.mark.parametrize(
        ("decision", "expected_event"),
        [("admit", "guest_admitted"), ("reject", "guest_rejected")],
    )
    def test_reconnecting_after_decision_receives_final_state(
        self,
        client,
        decision,
        expected_event,
    ):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Quinn"},
        ).json()
        client.post(
            f"/api/v1/meetings/{meeting_id}/lobby/{request['id']}/decision",
            json={"decision": decision},
        )

        with client.websocket_connect(
            f"/api/v1/meetings/{meeting_id}/lobby-ws?guest_request_id={request['id']}"
        ) as websocket:
            event = websocket.receive_json()

        assert event["type"] == expected_event
        assert event["guest_request_id"] == request["id"]

    def test_attendance_is_created_only_by_step_three_join_and_linked_once(self, client, db_session):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Rae", "guest_email": "rae@example.com"},
        ).json()
        client.post(
            f"/api/v1/meetings/{meeting_id}/lobby/{request['id']}/decision",
            json={"decision": "admit"},
        )

        assert db_session.query(MeetingAttendanceLog).filter_by(meeting_id=meeting_id).count() == 0

        payload = {
            "participant_name": "Ignored client name",
            "guest_request_id": request["id"],
        }
        first_join = client.post(
            f"/api/v1/meetings/{meeting_id}/attendance/join",
            json=payload,
        )
        second_join = client.post(
            f"/api/v1/meetings/{meeting_id}/attendance/join",
            json=payload,
        )

        assert first_join.status_code == 201
        assert second_join.status_code == 201
        assert first_join.json()["id"] == second_join.json()["id"]
        assert first_join.json()["participant_name"] == "Rae"
        assert first_join.json()["is_external_guest"] is True
        assert db_session.query(MeetingAttendanceLog).filter_by(meeting_id=meeting_id).count() == 1

    def test_decision_requires_meeting_demo_organizer_ownership(self, client, db_session):
        meeting = _future_meeting(client)
        meeting_id = meeting["id"]
        request = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby",
            json={"guest_name": "Sam"},
        ).json()
        db_session.query(Meeting).filter_by(id=meeting_id).update(
            {Meeting.organizer_id: "another-organizer"}
        )
        db_session.commit()

        response = client.post(
            f"/api/v1/meetings/{meeting_id}/lobby/{request['id']}/decision",
            json={"decision": "admit"},
        )

        assert response.status_code == 403
