import unittest
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from main import app
from app.db.database import SessionLocal, create_tables
from app.meeting.models import Meeting


class TestMeetingValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_tables()
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        self.created_meeting_ids: list[str] = []

    def tearDown(self):
        # Clean up any created test rows so no test data persists
        for meeting_id in self.created_meeting_ids:
            self.db.query(Meeting).filter(Meeting.id == meeting_id).delete()
        self.db.commit()
        self.db.close()

    def test_nonexistent_meeting_returns_404(self):
        random_id = str(uuid.uuid4())
        response = self.client.get(f"/api/v1/meetings/{random_id}/validate")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Meeting not found"})

    def test_valid_meeting_returns_200_and_payload(self):
        meeting_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        meeting = Meeting(
            id=meeting_id,
            organizer_id="test-organizer-secret",
            title="Sprint Planning",
            agenda="Discuss sprint goals",
            start_at=now - timedelta(minutes=10),
            end_at=now + timedelta(hours=1),
            join_url=f"http://localhost:5173/meetings/{meeting_id}",
            status="scheduled",
        )
        self.db.add(meeting)
        self.db.commit()
        self.created_meeting_ids.append(meeting_id)

        response = self.client.get(f"/api/v1/meetings/{meeting_id}/validate")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], meeting_id)
        self.assertEqual(data["title"], "Sprint Planning")
        self.assertEqual(data["agenda"], "Discuss sprint goals")
        self.assertEqual(data["status"], "scheduled")
        self.assertEqual(data["organizer_id"], "test-organizer-secret")
        self.assertTrue(data["is_valid"])
        self.assertFalse(data["is_expired"])
        # Keep the unrelated join URL private while exposing organizer identity for host UI.
        self.assertNotIn("join_url", data)

    def test_expired_meeting_returns_410(self):
        meeting_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        meeting = Meeting(
            id=meeting_id,
            organizer_id="test-organizer-secret",
            title="Past Retro",
            agenda="Old retrospective",
            start_at=now - timedelta(hours=2),
            end_at=now - timedelta(minutes=15),
            join_url=f"http://localhost:5173/meetings/{meeting_id}",
            status="scheduled",
        )
        self.db.add(meeting)
        self.db.commit()
        self.created_meeting_ids.append(meeting_id)

        response = self.client.get(f"/api/v1/meetings/{meeting_id}/validate")

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json(), {"detail": "Meeting has expired"})

    def test_get_meeting_by_id_endpoint_alias(self):
        meeting_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        meeting = Meeting(
            id=meeting_id,
            organizer_id="test-organizer-secret",
            title="Alias Test Meeting",
            agenda="Checking /meetings/{id}",
            start_at=now - timedelta(minutes=5),
            end_at=now + timedelta(hours=1),
            join_url=f"http://localhost:5173/meetings/{meeting_id}",
            status="scheduled",
        )
        self.db.add(meeting)
        self.db.commit()
        self.created_meeting_ids.append(meeting_id)

        response = self.client.get(f"/api/v1/meetings/{meeting_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], meeting_id)
        self.assertEqual(response.json()["organizer_id"], "test-organizer-secret")


if __name__ == "__main__":
    unittest.main()
