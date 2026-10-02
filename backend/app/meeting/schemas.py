from datetime import datetime
from pydantic import BaseModel, Field, model_validator


class ParticipantCreate(BaseModel):
    user_id: str | None = None
    email: str | None = None


class MeetingCreate(BaseModel):
    title: str = Field(..., min_length=1)
    agenda: str | None = None
    meeting_type: str = Field(default="scheduled")
    start_at: datetime
    end_at: datetime
    participants: list[ParticipantCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_meeting(self):
        if self.meeting_type not in ["instant", "scheduled"]:
            raise ValueError("Meeting type must be instant or scheduled")

        if self.end_at <= self.start_at:
            raise ValueError("End time must be after start time")

        return self


class MeetingResponse(BaseModel):
    id: str
    title: str
    agenda: str | None
    start_at: datetime
    end_at: datetime
    join_url: str
    status: str