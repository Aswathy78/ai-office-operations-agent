import json
import logging
import uuid
from typing import Any
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    In-memory WebSocket connection manager for WebRTC signaling.
    Maintains isolated rooms grouped by meeting_id.
    """

    def __init__(self):
        # meeting_id -> {connection_id: WebSocket}
        self.rooms: dict[str, dict[str, WebSocket]] = {}

    async def connect(
        self,
        meeting_id: str,
        websocket: WebSocket,
        connection_id: str | None = None,
    ) -> str:
        await websocket.accept()

        conn_id = connection_id or str(uuid.uuid4())

        if meeting_id not in self.rooms:
            self.rooms[meeting_id] = {}

        self.rooms[meeting_id][conn_id] = websocket
        logger.info("Participant %s joined meeting %s", conn_id, meeting_id)
        return conn_id

    def disconnect(self, meeting_id: str, connection_id: str) -> None:
        if meeting_id in self.rooms and connection_id in self.rooms[meeting_id]:
            del self.rooms[meeting_id][connection_id]
            logger.info("Participant %s removed from meeting %s", connection_id, meeting_id)

            if not self.rooms[meeting_id]:
                del self.rooms[meeting_id]

    def get_room_participants(
        self,
        meeting_id: str,
        exclude_id: str | None = None,
    ) -> list[str]:
        if meeting_id not in self.rooms:
            return []
        return [
            conn_id
            for conn_id in self.rooms[meeting_id].keys()
            if conn_id != exclude_id
        ]

    def is_participant_in_meeting(self, meeting_id: str, connection_id: str) -> bool:
        return (
            meeting_id in self.rooms
            and connection_id in self.rooms[meeting_id]
        )

    async def send_personal_message(
        self,
        meeting_id: str,
        target_id: str,
        message: dict[str, Any],
    ) -> bool:
        if meeting_id in self.rooms and target_id in self.rooms[meeting_id]:
            websocket = self.rooms[meeting_id][target_id]
            try:
                await websocket.send_json(message)
                return True
            except Exception as e:
                logger.warning("Failed to send message to %s: %s", target_id, e)
                return False
        return False

    async def broadcast(
        self,
        meeting_id: str,
        message: dict[str, Any],
        exclude_id: str | None = None,
    ) -> None:
        if meeting_id not in self.rooms:
            return

        # Snapshot items to avoid dictionary mutation during iteration
        connections = list(self.rooms[meeting_id].items())
        for conn_id, websocket in connections:
            if conn_id == exclude_id:
                continue
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.warning("Failed to broadcast to %s: %s", conn_id, e)

    async def close_room(
        self,
        meeting_id: str,
        code: int = 1000,
        reason: str = "Meeting ended",
    ) -> None:
        if meeting_id not in self.rooms:
            return

        connections = list(self.rooms[meeting_id].items())
        for conn_id, websocket in connections:
            try:
                await websocket.close(code=code, reason=reason)
            except Exception as e:
                logger.warning("Failed to close socket for %s: %s", conn_id, e)

        if meeting_id in self.rooms:
            del self.rooms[meeting_id]


signaling_manager = ConnectionManager()
