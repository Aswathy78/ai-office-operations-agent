from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.meeting.schemas import MeetingCreate, MeetingResponse
from app.meeting.service import create_meeting, get_user_meetings

router = APIRouter(
    prefix="/api/v1/meetings",
    tags=["Meetings"],
)


@router.post("/", response_model=MeetingResponse)
def create_new_meeting(
    meeting: MeetingCreate,
    db: Session = Depends(get_db),
):
    return create_meeting(
        db=db,
        meeting_data=meeting,
        organizer_id="demo-organizer",
    )

@router.get("/", response_model=list[MeetingResponse])
def get_meetings(
    db: Session = Depends(get_db),
    email: str | None = None,
):
    return get_user_meetings(
        db=db,
        user_id="demo-organizer",
        email=email,
    )