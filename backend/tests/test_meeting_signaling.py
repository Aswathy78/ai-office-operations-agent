import unittest
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from main import app
from app.db.database import SessionLocal, create_tables
from app.meeting.models import Meeting
from app.meeting.signaling import signaling_manager


class TestMeetingSignaling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_tables()
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        self.created_meeting_ids: list[str] = []

    def tearDown(self):
        for meeting_id in self.created_meeting_ids:
            self.db.query(Meeting).filter(Meeting.id == meeting_id).delete()
        self.db.commit()
        self.db.close()
        # Clean up any leftover signaling state in memory
        signaling_manager.rooms.clear()

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
            organizer_id="org-signaling-test",
            title="Signaling Test Meeting",
            agenda="Testing WebSockets",
            start_at=start_at,
            end_at=end_at,
            join_url=f"http://localhost:5173/meetings/{meeting_id}",
            status="scheduled",
        )
        self.db.add(meeting)
        self.db.commit()
        self.created_meeting_ids.append(meeting_id)
        return meeting

    def test_invalid_meeting_is_rejected(self):
        fake_meeting_id = str(uuid.uuid4())
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(f"/api/v1/meetings/{fake_meeting_id}/ws") as _:
                pass
        self.assertEqual(cm.exception.code, 1008)

    def test_expired_meeting_is_rejected(self):
        meeting = self._create_meeting(expired=True)
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as _:
                pass
        self.assertEqual(cm.exception.code, 1008)

    def test_valid_meeting_connects_and_receives_welcome(self):
        meeting = self._create_meeting(expired=False)
        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws:
            welcome = ws.receive_json()
            self.assertEqual(welcome["type"], "welcome")
            self.assertEqual(welcome["sender_id"], "server")
            self.assertEqual(welcome["payload"]["meeting_id"], meeting.id)
            self.assertTrue(welcome["payload"]["connection_id"])
            self.assertEqual(welcome["payload"]["participants"], [])

    def test_signaling_messages_forwarded_to_intended_participant(self):
        meeting = self._create_meeting(expired=False)

        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws1:
            welcome1 = ws1.receive_json()
            user1_id = welcome1["payload"]["connection_id"]

            with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws2:
                welcome2 = ws2.receive_json()
                user2_id = welcome2["payload"]["connection_id"]
                # User 2 should discover User 1 in existing participants
                self.assertIn(user1_id, welcome2["payload"]["participants"])

                # User 1 should receive participant_joined for User 2
                joined_event = ws1.receive_json()
                self.assertEqual(joined_event["type"], "participant_joined")
                self.assertEqual(joined_event["payload"]["connection_id"], user2_id)

                # User 1 sends WebRTC offer to User 2
                offer_payload = {"type": "offer", "sdp": "v=0\r\no=alice ..."}
                ws1.send_json({
                    "type": "offer",
                    "target_id": user2_id,
                    "payload": offer_payload,
                })

                # User 2 receives the forwarded offer
                received_offer = ws2.receive_json()
                self.assertEqual(received_offer["type"], "offer")
                self.assertEqual(received_offer["sender_id"], user1_id)
                self.assertEqual(received_offer["target_id"], user2_id)
                self.assertEqual(received_offer["payload"], offer_payload)

                # User 2 sends WebRTC answer to User 1
                answer_payload = {"type": "answer", "sdp": "v=0\r\no=bob ..."}
                ws2.send_json({
                    "type": "answer",
                    "target_id": user1_id,
                    "payload": answer_payload,
                })

                # User 1 receives the forwarded answer
                received_answer = ws1.receive_json()
                self.assertEqual(received_answer["type"], "answer")
                self.assertEqual(received_answer["sender_id"], user2_id)
                self.assertEqual(received_answer["target_id"], user1_id)
                self.assertEqual(received_answer["payload"], answer_payload)

                # User 1 sends ICE candidate to User 2
                ice_payload = {"candidate": "candidate:1 1 UDP ...", "sdpMLineIndex": 0}
                ws1.send_json({
                    "type": "ice_candidate",
                    "target_id": user2_id,
                    "payload": ice_payload,
                })

                # User 2 receives ICE candidate
                received_ice = ws2.receive_json()
                self.assertEqual(received_ice["type"], "ice_candidate")
                self.assertEqual(received_ice["sender_id"], user1_id)
                self.assertEqual(received_ice["payload"], ice_payload)

    def test_three_participants_keep_independent_targeted_signaling(self):
        meeting = self._create_meeting(expired=False)

        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws1:
            user1_id = ws1.receive_json()["payload"]["connection_id"]

            with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws2:
                welcome2 = ws2.receive_json()
                user2_id = welcome2["payload"]["connection_id"]
                self.assertEqual(welcome2["payload"]["participants"], [user1_id])
                self.assertEqual(ws1.receive_json()["type"], "participant_joined")

                with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws3:
                    welcome3 = ws3.receive_json()
                    user3_id = welcome3["payload"]["connection_id"]
                    self.assertCountEqual(
                        welcome3["payload"]["participants"],
                        [user1_id, user2_id],
                    )
                    self.assertEqual(ws1.receive_json()["payload"]["connection_id"], user3_id)
                    self.assertEqual(ws2.receive_json()["payload"]["connection_id"], user3_id)

                    offer_to_third_peer = {"type": "offer", "sdp": "offer-for-third-peer"}
                    ws1.send_json({
                        "type": "offer",
                        "target_id": user3_id,
                        "payload": offer_to_third_peer,
                    })
                    received_offer = ws3.receive_json()
                    self.assertEqual(received_offer["sender_id"], user1_id)
                    self.assertEqual(received_offer["target_id"], user3_id)
                    self.assertEqual(received_offer["payload"], offer_to_third_peer)

                    answer_to_second_peer = {"type": "answer", "sdp": "answer-for-second-peer"}
                    ws3.send_json({
                        "type": "answer",
                        "target_id": user2_id,
                        "payload": answer_to_second_peer,
                    })
                    received_answer = ws2.receive_json()
                    self.assertEqual(received_answer["sender_id"], user3_id)
                    self.assertEqual(received_answer["target_id"], user2_id)
                left_from_ws1 = ws1.receive_json()
                left_from_ws2 = ws2.receive_json()
                self.assertEqual(left_from_ws1["type"], "participant_left")
                self.assertEqual(left_from_ws1["payload"]["connection_id"], user3_id)
                self.assertEqual(left_from_ws2["type"], "participant_left")
                self.assertEqual(left_from_ws2["payload"]["connection_id"], user3_id)

                remaining_peer_offer = {"type": "offer", "sdp": "remaining-peers-still-connected"}
                ws1.send_json({
                    "type": "offer",
                    "target_id": user2_id,
                    "payload": remaining_peer_offer,
                })
                self.assertEqual(ws2.receive_json()["payload"], remaining_peer_offer)

    def test_meeting_isolation(self):
        meeting1 = self._create_meeting(expired=False)
        meeting2 = self._create_meeting(expired=False)

        with self.client.websocket_connect(f"/api/v1/meetings/{meeting1.id}/ws") as ws1:
            welcome1 = ws1.receive_json()
            user1_id = welcome1["payload"]["connection_id"]

            with self.client.websocket_connect(f"/api/v1/meetings/{meeting2.id}/ws") as ws2:
                welcome2 = ws2.receive_json()
                user2_id = welcome2["payload"]["connection_id"]

                # Meeting 2 does not list user 1
                self.assertEqual(welcome2["payload"]["participants"], [])

                # User 1 tries to send message to User 2 (who is in meeting 2)
                ws1.send_json({
                    "type": "offer",
                    "target_id": user2_id,
                    "payload": {"sdp": "isolation-test"},
                })

                # User 1 receives error because target is not in meeting 1
                err = ws1.receive_json()
                self.assertEqual(err["type"], "error")
                self.assertIn("not found in meeting", err["payload"]["detail"])

    def test_disconnect_removes_participant_and_notifies_remaining(self):
        meeting = self._create_meeting(expired=False)

        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws1:
            welcome1 = ws1.receive_json()

            with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws2:
                welcome2 = ws2.receive_json()
                user2_id = welcome2["payload"]["connection_id"]
                # Flush joined message on ws1
                _ = ws1.receive_json()

            # Now ws2 has disconnected (exited context)
            leave_event = ws1.receive_json()
            self.assertEqual(leave_event["type"], "participant_left")
            self.assertEqual(leave_event["payload"]["connection_id"], user2_id)

    def test_malformed_messages_do_not_crash_server(self):
        meeting = self._create_meeting(expired=False)

        with self.client.websocket_connect(f"/api/v1/meetings/{meeting.id}/ws") as ws:
            _ = ws.receive_json()  # welcome

            # 1. Invalid JSON string
            ws.send_text("this-is-not-json")
            err1 = ws.receive_json()
            self.assertEqual(err1["type"], "error")
            self.assertIn("must be valid JSON", err1["payload"]["detail"])

            # 2. Missing type
            ws.send_json({"payload": {"some": "data"}})
            err2 = ws.receive_json()
            self.assertEqual(err2["type"], "error")
            self.assertIn("missing or invalid 'type'", err2["payload"]["detail"])

            # 3. Unsupported type
            ws.send_json({"type": "unknown_action"})
            err3 = ws.receive_json()
            self.assertEqual(err3["type"], "error")
            self.assertIn("Unsupported message type", err3["payload"]["detail"])

            # 4. Valid join acknowledgment still works on same connection
            ws.send_json({"type": "join"})
            ack = ws.receive_json()
            self.assertEqual(ack["type"], "join_ack")


if __name__ == "__main__":
    unittest.main()
