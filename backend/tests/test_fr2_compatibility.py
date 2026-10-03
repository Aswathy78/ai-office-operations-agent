import unittest
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from main import app
from app.db.database import SessionLocal, create_tables
from app.meeting.models import Meeting, MeetingParticipant


class TestFR2Compatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_tables()
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        self.created_meeting_ids: list[str] = []

    def tearDown(self):
        for meeting_id in self.created_meeting_ids:
            self.db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).delete()
            self.db.query(Meeting).filter(Meeting.id == meeting_id).delete()
        self.db.commit()
        self.db.close()

    def test_fr2_create_meeting_still_works(self):
        now = datetime.now(timezone.utc)
        payload = {
            "title": "FR2 Compatibility Test",
            "agenda": "Ensure create meeting is unaffected",
            "meeting_type": "scheduled",
            "start_at": (now + timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=2)).isoformat(),
            "participants": [
                {"email": "fr2_test@example.com"}
            ]
        }

        response = self.client.post("/api/v1/meetings/", json=payload)
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["title"], "FR2 Compatibility Test")
        self.assertEqual(data["status"], "scheduled")
        self.assertTrue(data["join_url"].startswith("http://localhost:5173/meetings/"))
        self.created_meeting_ids.append(data["id"])

        # Verify it can be listed via GET /api/v1/meetings/?email=fr2_test@example.com
        list_response = self.client.get("/api/v1/meetings/?email=fr2_test@example.com")
        self.assertEqual(list_response.status_code, 200)
        meetings = list_response.json()
        self.assertTrue(any(m["id"] == data["id"] for m in meetings))

    def test_organizer_can_list_meeting_without_invitees(self):
        now = datetime.now(timezone.utc)
        response = self.client.post(
            "/api/v1/meetings/",
            json={
                "title": "Organizer Only Meeting",
                "start_at": (now + timedelta(hours=1)).isoformat(),
                "end_at": (now + timedelta(hours=2)).isoformat(),
                "participants": [],
            },
        )
        self.assertEqual(response.status_code, 200)
        meeting_id = response.json()["id"]
        self.created_meeting_ids.append(meeting_id)

        list_response = self.client.get("/api/v1/meetings/")
        email_list_response = self.client.get("/api/v1/meetings/?email=employee@example.com")

        self.assertEqual(list_response.status_code, 200)
        self.assertTrue(any(meeting["id"] == meeting_id for meeting in list_response.json()))
        self.assertEqual(email_list_response.status_code, 200)
        self.assertTrue(any(meeting["id"] == meeting_id for meeting in email_list_response.json()))

    def test_role_identity_lists_owned_and_invited_meetings(self):
        now = datetime.now(timezone.utc)
        response = self.client.post(
            "/api/v1/meetings/?organizer_id=user-002",
            json={
                "title": "Role Identity Meeting",
                "start_at": (now + timedelta(hours=1)).isoformat(),
                "end_at": (now + timedelta(hours=2)).isoformat(),
                "participants": [{"email": "rahul@example.com"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        meeting_id = response.json()["id"]
        self.created_meeting_ids.append(meeting_id)

        organizer_meetings = self.client.get(
            "/api/v1/meetings/?user_id=user-002&email=manager@example.com"
        ).json()
        invited_meetings = self.client.get(
            "/api/v1/meetings/?user_id=user-001&email=rahul@example.com"
        ).json()
        unrelated_meetings = self.client.get(
            "/api/v1/meetings/?user_id=user-003&email=hr.operations@example.com"
        ).json()

        self.assertTrue(any(meeting["id"] == meeting_id for meeting in organizer_meetings))
        self.assertTrue(any(meeting["id"] == meeting_id for meeting in invited_meetings))
        self.assertFalse(any(meeting["id"] == meeting_id for meeting in unrelated_meetings))


if __name__ == "__main__":
    unittest.main()
