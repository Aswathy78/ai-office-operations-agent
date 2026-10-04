import unittest
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from main import app
from app.db.database import SessionLocal, create_tables
from app.meeting.models import (
    Meeting,
    MeetingAttendanceLog,
    MeetingGuestRequest,
    MeetingParticipant,
    MeetingRecording,
    MeetingTranscript,
    GUEST_STATUS_ADMITTED,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)
from app.meeting.signaling import signaling_manager


class TestMeetingDeletionAndLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_tables()
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        self.created_meeting_ids: list[str] = []

    def tearDown(self):
        for meeting_id in self.created_meeting_ids:
            self.db.query(MeetingGuestRequest).filter(MeetingGuestRequest.meeting_id == meeting_id).delete()
            self.db.query(MeetingAttendanceLog).filter(MeetingAttendanceLog.meeting_id == meeting_id).delete()
            self.db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).delete()
            recording_ids = [
                row[0]
                for row in self.db.query(MeetingRecording.id)
                .filter(MeetingRecording.meeting_id == meeting_id)
                .all()
            ]
            if recording_ids:
                self.db.query(MeetingTranscript).filter(
                    MeetingTranscript.recording_id.in_(recording_ids)
                ).delete(synchronize_session=False)
            self.db.query(MeetingRecording).filter(
                MeetingRecording.meeting_id == meeting_id
            ).delete(synchronize_session=False)
            self.db.query(Meeting).filter(Meeting.id == meeting_id).delete()
        self.db.commit()
        self.db.close()
        signaling_manager.rooms.clear()

    def _create_meeting(
        self,
        organizer_id: str = "user-001",
        status: str = "scheduled",
        expired: bool = False,
    ) -> Meeting:
        meeting_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        if expired:
            start_at = now - timedelta(hours=2)
            end_at = now - timedelta(minutes=15)
        else:
            start_at = now - timedelta(minutes=5)
            end_at = now + timedelta(hours=1)

        meeting = Meeting(
            id=meeting_id,
            organizer_id=organizer_id,
            title="Lifecycle & Deletion Test Meeting",
            agenda="Testing deletion requirements",
            start_at=start_at,
            end_at=end_at,
            join_url=f"http://localhost:5173/meetings/{meeting_id}",
            status=status,
        )
        self.db.add(meeting)
        self.db.commit()
        self.created_meeting_ids.append(meeting_id)
        return meeting

    def test_completed_meeting_can_be_deleted_by_organizer(self):
        meeting = self._create_meeting(organizer_id="user-001", status="completed")
        meeting_id = meeting.id
        res = self.client.delete(f"/api/v1/meetings/{meeting_id}?organizer_id=user-001")
        self.assertEqual(res.status_code, 204)

        # Verify meeting record is gone
        found = self.db.query(Meeting).filter(Meeting.id == meeting_id).first()
        self.assertIsNone(found)

    def test_expired_meeting_with_stale_status_auto_completes_and_is_deletable(self):
        # Meeting whose end_at is in the past, but status was left as 'scheduled' or 'instant'
        meeting = self._create_meeting(organizer_id="user-001", status="scheduled", expired=True)
        meeting_id = meeting.id

        # Deleting it by organizer should auto-repair status and delete successfully
        res = self.client.delete(f"/api/v1/meetings/{meeting_id}?organizer_id=user-001")
        self.assertEqual(res.status_code, 204)

        found = self.db.query(Meeting).filter(Meeting.id == meeting_id).first()
        self.assertIsNone(found)

    def test_active_meeting_with_connected_participants_rejects_deletion(self):
        meeting = self._create_meeting(organizer_id="user-001", status="active", expired=False)

        # Connect a participant to the signaling room
        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as _:
            # Attempt to delete while participant is in the meeting
            res = self.client.delete(f"/api/v1/meetings/{meeting.id}?organizer_id=user-001")
            self.assertEqual(res.status_code, 400)
            self.assertIn("active", res.json()["detail"].lower())

    def test_non_organizer_cannot_delete_meeting(self):
        meeting = self._create_meeting(organizer_id="user-001", status="completed")
        # user-002 attempts to delete user-001's meeting
        res = self.client.delete(f"/api/v1/meetings/{meeting.id}?organizer_id=user-002")
        self.assertEqual(res.status_code, 403)
        self.assertIn("Only the meeting organizer", res.json()["detail"])

        # Verify meeting still exists
        self.db.expire_all()
        found = self.db.query(Meeting).filter(Meeting.id == meeting.id).first()
        self.assertIsNotNone(found)

    def test_meeting_completes_when_participants_leave_and_is_deletable(self):
        meeting = self._create_meeting(organizer_id="user-001", status="instant", expired=False)

        # Organizer joins attendance
        join_res = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json={"participant_name": "Rahul", "user_id": "user-001"},
        )
        self.assertEqual(join_res.status_code, 201)
        att_id = join_res.json()["id"]

        # Connect WS
        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws?user_id=user-001") as _:
            pass

        # Participant leaves attendance
        leave_res = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/{att_id}/leave",
        )
        self.assertEqual(leave_res.status_code, 200)

        # Meeting should now be completed
        self.db.expire_all()
        m = self.db.query(Meeting).filter(Meeting.id == meeting.id).first()
        self.assertEqual(m.status, "completed")

        # Report is accessible before deletion
        report_res = self.client.get(f"/api/v1/meetings/{meeting.id}/report?organizer_id=user-001")
        self.assertEqual(report_res.status_code, 200)
        self.assertEqual(len(report_res.json()["attendance"]), 1)
        self.assertIsNotNone(report_res.json()["attendance"][0]["left_at"])

        # Delete meeting
        del_res = self.client.delete(f"/api/v1/meetings/{meeting.id}?organizer_id=user-001")
        self.assertEqual(del_res.status_code, 204)

        # Joining or validating deleted meeting should return 404
        val_res = self.client.get(f"/api/v1/meetings/{meeting.id}/validate")
        self.assertEqual(val_res.status_code, 404)

        # Report for deleted meeting should return 404
        report_after = self.client.get(f"/api/v1/meetings/{meeting.id}/report?organizer_id=user-001")
        self.assertEqual(report_after.status_code, 404)

    def test_organizer_ends_meeting_for_everyone_then_deletes(self):
        meeting = self._create_meeting(organizer_id="user-001", status="active", expired=False)

        # Two participants join attendance
        att1 = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json={"participant_name": "Rahul", "user_id": "user-001"},
        ).json()["id"]

        att2 = self.client.post(
            f"/api/v1/meetings/{meeting.id}/attendance/join",
            json={"participant_name": "Guest John", "is_external_guest": True},
        ).json()["id"]

        # Organizer ends meeting for everyone
        end_res = self.client.post(f"/api/v1/meetings/{meeting.id}/end?organizer_id=user-001")
        self.assertEqual(end_res.status_code, 200)
        self.assertEqual(end_res.json()["status"], "completed")

        # Attendance logs should all have left_at set
        self.db.expire_all()
        logs = self.db.query(MeetingAttendanceLog).filter(MeetingAttendanceLog.meeting_id == meeting.id).all()
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertIsNotNone(log.left_at)

        # Organizer can delete
        del_res = self.client.delete(f"/api/v1/meetings/{meeting.id}?organizer_id=user-001")
        self.assertEqual(del_res.status_code, 204)

    def test_delete_cleans_up_all_child_records(self):
        meeting = self._create_meeting(organizer_id="user-001", status="completed")

        # Add participant record
        part = MeetingParticipant(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            user_id="user-002",
            email="maya@example.com",
            role="participant",
        )
        self.db.add(part)

        # Add attendance log
        att = MeetingAttendanceLog(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            participant_name="Maya",
            joined_at=datetime.now(timezone.utc),
            left_at=datetime.now(timezone.utc),
        )
        self.db.add(att)

        # Add guest request
        g_req = MeetingGuestRequest(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            guest_name="Guest Bob",
            guest_email="bob@example.com",
            status=GUEST_STATUS_ADMITTED,
            attendance_log_id=att.id,
        )
        recording = MeetingRecording(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            status=RECORDING_STATUS_COMPLETED,
        )
        transcript = MeetingTranscript(
            id=str(uuid.uuid4()),
            recording_id=recording.id,
            status=TRANSCRIPT_STATUS_COMPLETED,
            language="en",
            transcript_text="Meeting transcript for deletion test",
        )
        recording_id = recording.id
        self.db.add_all([g_req, recording, transcript])
        self.db.commit()

        meeting_id = meeting.id
        # Delete
        del_res = self.client.delete(f"/api/v1/meetings/{meeting_id}?organizer_id=user-001")
        self.assertEqual(del_res.status_code, 204)

        # Verify all child rows are gone
        self.assertEqual(self.db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).count(), 0)
        self.assertEqual(self.db.query(MeetingAttendanceLog).filter(MeetingAttendanceLog.meeting_id == meeting_id).count(), 0)
        self.assertEqual(self.db.query(MeetingGuestRequest).filter(MeetingGuestRequest.meeting_id == meeting_id).count(), 0)
        self.assertEqual(self.db.query(MeetingRecording).filter(MeetingRecording.meeting_id == meeting_id).count(), 0)
        self.assertEqual(self.db.query(MeetingTranscript).filter_by(recording_id=recording_id).count(), 0)


if __name__ == "__main__":
    unittest.main()
