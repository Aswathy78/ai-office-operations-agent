import unittest
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from main import app
from app.db.database import SessionLocal, create_tables
from app.meeting.models import Meeting, MeetingAttendanceLog


class TestMeetingAttendance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_tables()
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        self.created_meeting_ids: list[str] = []

    def tearDown(self):
        # Clean up created test data so tests remain isolated
        for meeting_id in self.created_meeting_ids:
            self.db.query(MeetingAttendanceLog).filter(MeetingAttendanceLog.meeting_id == meeting_id).delete()
            self.db.query(Meeting).filter(Meeting.id == meeting_id).delete()
        self.db.commit()
        self.db.close()

    def _create_meeting(self, expired: bool = False) -> Meeting:
        meeting_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        if expired:
            start_at = now - timedelta(hours=2)
            end_at = now - timedelta(minutes=10)
        else:
            start_at = now - timedelta(minutes=5)
            end_at = now + timedelta(hours=1)

        meeting = Meeting(
            id=meeting_id,
            organizer_id="org-test-123",
            title="Attendance Test Meeting",
            agenda="Testing attendance APIs",
            start_at=start_at,
            end_at=end_at,
            join_url=f"http://localhost:5173/meetings/{meeting_id}",
            status="scheduled",
        )
        self.db.add(meeting)
        self.db.commit()
        self.created_meeting_ids.append(meeting_id)
        return meeting

    def test_1_valid_meeting_participant_joins(self):
        meeting = self._create_meeting(expired=False)

        join_payload = {
            "participant_name": "Alice Developer",
            "participant_email": "alice@example.com",
            "user_id": "usr-alice-01",
            "is_external_guest": False,
        }

        response = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json=join_payload,
        )

        self.assertIn(response.status_code, [200, 201])
        data = response.json()
        self.assertTrue(data["id"])
        self.assertEqual(data["meeting_id"], meeting.id)
        self.assertEqual(data["participant_name"], "Alice Developer")
        self.assertEqual(data["participant_email"], "alice@example.com")
        self.assertEqual(data["user_id"], "usr-alice-01")
        self.assertFalse(data["is_external_guest"])
        self.assertIsNotNone(data["joined_at"])
        self.assertIsNone(data["left_at"])

    def test_2_participant_leaves(self):
        meeting = self._create_meeting(expired=False)

        join_response = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json={
                "participant_name": "Bob Designer",
                "participant_email": "bob@example.com",
                "is_external_guest": True,
            },
        )
        attendance_id = join_response.json()["id"]

        leave_response = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/{attendance_id}/leave",
        )

        self.assertEqual(leave_response.status_code, 200)
        data = leave_response.json()
        self.assertEqual(data["id"], attendance_id)
        self.assertIsNotNone(data["left_at"])

        # Test leaving an already left record is idempotent
        re_leave = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/{attendance_id}/leave",
        )
        self.assertEqual(re_leave.status_code, 200)
        self.assertEqual(re_leave.json()["left_at"], data["left_at"])

    def test_3_nonexistent_meeting_join_and_leave_returns_404(self):
        fake_meeting_id = str(uuid.uuid4())
        fake_attendance_id = str(uuid.uuid4())

        join_response = self.client.post(
            f"/api/v1/meetings/{fake_meeting_id}/attendance/join",
            json={"participant_name": "Ghost User"},
        )
        self.assertEqual(join_response.status_code, 404)
        self.assertEqual(join_response.json(), {"detail": "Meeting not found"})

        leave_response = self.client.post(
            f"/api/v1/meetings/{fake_meeting_id}/attendance/{fake_attendance_id}/leave",
        )
        self.assertEqual(leave_response.status_code, 404)
        self.assertEqual(leave_response.json(), {"detail": "Meeting not found"})

    def test_4_expired_meeting_join_returns_410(self):
        meeting = self._create_meeting(expired=True)

        response = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json={"participant_name": "Late Attendee"},
        )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json(), {"detail": "Meeting has expired"})

    def test_5_nonexistent_attendance_record_returns_404(self):
        meeting = self._create_meeting(expired=False)
        fake_attendance_id = str(uuid.uuid4())

        response = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/{fake_attendance_id}/leave",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Attendance record not found"})

    def test_6_attendance_record_from_another_meeting_cannot_be_closed(self):
        meeting_1 = self._create_meeting(expired=False)
        meeting_2 = self._create_meeting(expired=False)

        join_response = self.client.post(
            f"/api/v1/meetings/{meeting_1.id}/attendance/join",
            json={"participant_name": "Cross-Meeting Attendee"},
        )
        attendance_id_1 = join_response.json()["id"]

        # Attempt to leave meeting_2 with attendance_id from meeting_1
        response = self.client.post(
            f"/api/v1/meetings/{meeting_2.id}/attendance/{attendance_id_1}/leave",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {"detail": "Attendance record does not belong to this meeting"},
        )

    def test_duplicate_active_join_returns_existing_record(self):
        meeting = self._create_meeting(expired=False)

        join_payload = {
            "participant_name": "Charlie Engineer",
            "participant_email": "charlie@example.com",
            "user_id": "usr-charlie-01",
        }

        first_join = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json=join_payload,
        )
        self.assertEqual(first_join.status_code, 201)
        record_id_1 = first_join.json()["id"]

        # Re-joining while active returns the existing open record
        second_join = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json=join_payload,
        )
        self.assertEqual(second_join.status_code, 201)
        record_id_2 = second_join.json()["id"]

        self.assertEqual(record_id_1, record_id_2)

        # Confirm in DB that only 1 record exists
        count = self.db.query(MeetingAttendanceLog).filter(
            MeetingAttendanceLog.meeting_id == meeting.id,
            MeetingAttendanceLog.user_id == "usr-charlie-01",
        ).count()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
