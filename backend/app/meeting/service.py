import uuid

from sqlalchemy.orm import Session

from app.meeting.models import Meeting, MeetingParticipant
from app.meeting.schemas import MeetingCreate


def create_meeting(
    db: Session,
    meeting_data: MeetingCreate,
    organizer_id: str,
) -> Meeting:

    meeting_id = str(uuid.uuid4())

    join_url = f"http://localhost:5173/meetings/{meeting_id}"

    meeting = Meeting(
        id=meeting_id,
        organizer_id=organizer_id,
        title=meeting_data.title,
        agenda=meeting_data.agenda,
        start_at=meeting_data.start_at,
        end_at=meeting_data.end_at,
        join_url=join_url,
        status=meeting_data.meeting_type,
    )

    db.add(meeting)

    for participant in meeting_data.participants:
        participant_record = MeetingParticipant(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            user_id=participant.user_id,
            email=participant.email,
            role="participant",
        )

        db.add(participant_record)

    db.commit()
    db.refresh(meeting)

    return meeting

def get_user_meetings(
    db: Session,
    user_id: str | None = None,
    email: str | None = None,
) -> list[Meeting]:

    query = (
        db.query(Meeting)
        .join(
            MeetingParticipant,
            Meeting.id == MeetingParticipant.meeting_id,
        )
    )

    if email:
        query = query.filter(
            MeetingParticipant.email == email
        )
    elif user_id:
        query = query.filter(
            (Meeting.organizer_id == user_id)
            | (MeetingParticipant.user_id == user_id)
        )

    return query.distinct().all()