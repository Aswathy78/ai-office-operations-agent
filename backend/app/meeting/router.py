import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth_service import (
    AuthConfigurationError,
    AuthenticatedPrincipal,
    InvalidAccessToken,
    authenticate_access_token,
)
from app.authentication import (
    authenticated_actor_id,
    require_authenticated_actor_id,
    require_authenticated_principal,
)
from app.db.database import get_db
from app.meeting.models import (
    Employee,
    Meeting,
    MeetingParticipant,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingAttendanceLog,
    MeetingSummary,
    Task,
    MeetingRescheduleRequest,
    MeetingDelegate,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_APPROVAL_REQUIRED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_PROPOSED,
    MEETING_ACTION_ITEM_OWNERSHIP_STATUS_UNRESOLVED,
    ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    ACTION_ITEM_REVIEW_STATUS_REJECTED,
    TRANSCRIPT_STATUS_COMPLETED,
)
from app.meeting.schemas import (
    ActionItemReviewRequest,
    AttendanceJoinRequest,
    AttendanceReportRow,
    AttendanceResponse,
    GuestDecisionRequest,
    GuestJoinRequest,
    GuestRequestResponse,
    MeetingCreate,
    MeetingCreationResponse,
    MeetingCalendarAttendeeResponse,
    MeetingCalendarEventResponse,
    MeetingCalendarResponse,
    ActionItemOwnerConfirmationRequest,
    EmployeeResponse,
    MeetingActionItemResponse,
    MeetingActionItemsResponse,
    MeetingReportResponse,
    MeetingResponse,
    MeetingListResponse,
    MeetingValidationResponse,
    MeetingRecordingUploadResponse,
    ParticipantTrackRegisterResponse,
    ParticipantTracksRegisterRequest,
    RecordingConsentRequest,
    RecordingConsentResponse,
    MeetingRecordingResponse,
    MeetingSummaryResponse,
    MeetingTranscriptResponse,
    MeetingTranscriptUpdateRequest,
    TranscriptSegment,
    TranscriptCorrectionResponse,
    MeetingMinutesApproveRequest,
    MeetingMinutesDeliveryResponse,
    MeetingMinutesRecipientResponse,
    MeetingMinutesResponse,
    MeetingMinutesUpdateRequest,
    ConflictDetail,
    AlternativeSlotDetail,
    NaturalLanguageScheduleRequest,
    ScheduleConfirmationRequest,
    NaturalLanguageScheduleResponse,
    MeetingRescheduleRequestResponse,
    RescheduleDecisionRequest,
    RescheduleDecisionResponse,
    MeetingDelegateCreateRequest,
    MeetingDelegateResponse,
    MeetingAuditLogResponse,
    MeetingCancellationDecisionRequest,
    MeetingCancellationDecisionResponse,
    MeetingCancellationRequestCreate,
    MeetingCancellationRequestResponse,
    MeetingChangeResponse,
    MeetingParticipantBulkChangeRequest,
    MeetingParticipantBulkChangeResponse,
    MeetingParticipantChangeRequest,
    MeetingParticipantResponse,
)
from app.meeting.scheduling_service import (
    _fits_attendee_working_hours,
    find_alternative_slots,
    get_organization_timezone,
    send_meeting_invitations,
    schedule_meeting_from_natural_language,
    reschedule_meeting_from_natural_language,
    decide_reschedule_request,
)
from app.meeting.calendar_provider import CalendarEvent, get_calendar_provider
from app.meeting.service import (
    TERMINAL_STATUSES,
    _try_complete_meeting,
    add_meeting_delegate,
    get_meeting_delegates,
    create_meeting,
    decide_guest_request,
    delete_meeting,
    force_complete_meeting,
    get_guest_request_by_id,
    get_meeting_by_id,
    get_pending_guest_requests,
    get_user_meetings,
    is_meeting_expired,
    record_participant_join,
    record_participant_leave,
    record_meeting_recording_consent,
    get_meeting_recording,
    start_meeting_recording,
    stop_meeting_recording,
    upload_meeting_recording,
    register_participant_track_recordings,
    upload_participant_track_recording,
    request_guest_join,
    get_meeting_transcript,
    update_meeting_transcript,
    get_meeting_transcript_corrections,
    is_authorized_rescheduler,
)
from app.meeting.event_change_service import (
    add_meeting_participant,
    add_meeting_participants,
    attendee_count,
    decide_meeting_cancellation,
    list_meeting_audit,
    list_pending_cancellations,
    remove_meeting_participant,
    request_meeting_cancellation,
    get_meeting_participants,
)
from app.meeting.action_item_service import (
    MeetingActionItemExtractionError,
    generate_meeting_action_items,
    get_action_item_confidence_threshold,
    refresh_missing_employee_matches,
    review_action_item,
)
from app.meeting.minutes_service import (
    approve_meeting_minutes,
    generate_or_get_meeting_minutes,
    get_meeting_minutes,
    get_minutes_recipients,
    refresh_minutes_content,
    send_meeting_minutes_to_recipients,
    update_meeting_minutes_content,
)
from app.meeting.signaling import signaling_manager
from app.task_service import synchronize_confirmed_action_item_tasks



router = APIRouter(
    prefix="/api/v1/meetings",
    tags=["Meetings"],
)
DEMO_ORGANIZER_ID = "demo-organizer"


def _meeting_response(meeting: Meeting) -> MeetingResponse:
    return MeetingResponse(
        id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=_as_utc(meeting.start_at),
        end_at=_as_utc(meeting.end_at),
        join_url=meeting.join_url,
        status=meeting.status,
        organizer_id=meeting.organizer_id,
        timezone=str(get_organization_timezone()),
    )


def _is_assigned_task_viewer(
    db: Session,
    meeting_id: str,
    employee_email: str | None,
    transcript_id: str | None = None,
) -> bool:
    if not employee_email:
        return False
    query = (
        db.query(Task.id)
        .join(Employee, Task.assignee_id == Employee.id)
        .filter(
            Task.meeting_id == meeting_id,
            Employee.email == employee_email,
            Employee.is_active.is_(True),
        )
    )
    if transcript_id is not None:
        query = query.filter(Task.transcript_id == transcript_id)
    return query.first() is not None


@router.post("/", response_model=MeetingCreationResponse)
def create_new_meeting(
    meeting: MeetingCreate,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    if meeting.meeting_type == "scheduled":
        attendee_emails = list(dict.fromkeys(
            participant.email.strip().lower()
            for participant in meeting.participants
            if participant.email and participant.email.strip()
        ))
        for participant in meeting.participants:
            if participant.user_id:
                employee = db.query(Employee).filter(Employee.id == participant.user_id).first()
                if employee and employee.email:
                    attendee_emails.append(employee.email.lower())
        attendee_emails = list(dict.fromkeys(attendee_emails))
        if attendee_emails:
            provider = get_calendar_provider()
            organization_timezone = get_organization_timezone()
            employee_ids = [
                participant.user_id
                for participant in meeting.participants
                if participant.user_id
            ]
            if attendee_emails:
                employee_ids.extend(
                    employee_id
                    for (employee_id,) in db.query(Employee.id)
                    .filter(Employee.email.in_(attendee_emails))
                    .all()
                )
            employee_ids = list(dict.fromkeys(employee_ids))
            employees_by_email = {
                employee.email.lower(): employee
                for employee in (
                    db.query(Employee).filter(Employee.id.in_(employee_ids)).all()
                    if employee_ids
                    else []
                )
                if employee.email and employee.email.lower() in attendee_emails
            }
            start_utc = meeting.start_at.replace(tzinfo=timezone.utc)
            end_utc = meeting.end_at.replace(tzinfo=timezone.utc)
            if not _fits_attendee_working_hours(
                start_utc,
                end_utc,
                attendee_emails,
                employees_by_email,
                organization_timezone,
            ):
                alternatives = find_alternative_slots(
                    db,
                    start_utc,
                    int((end_utc - start_utc).total_seconds() // 60),
                    attendee_emails,
                    provider=provider,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "The selected meeting time is outside attendee working hours.",
                        "conflicts": [
                            {
                                "attendee_email": email,
                                "attendee_name": employees_by_email[email].name if email in employees_by_email else None,
                                "conflicting_event_title": "Outside working hours",
                                "conflicting_start_at": start_utc.isoformat(),
                                "conflicting_end_at": end_utc.isoformat(),
                            }
                            for email in attendee_emails
                        ],
                        "alternative_slots": [
                            {"start_at": start.isoformat(), "end_at": end.isoformat()}
                            for start, end in alternatives
                        ],
                    },
                )
            availability = provider.check_availability(
                db, attendee_emails, start_utc, end_utc
            )
            if availability.has_conflicts:
                alternatives = find_alternative_slots(
                    db,
                    meeting.start_at,
                    int((meeting.end_at - meeting.start_at).total_seconds() // 60),
                    attendee_emails,
                    provider=provider,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "The selected meeting time conflicts with attendee availability.",
                        "conflicts": [
                            ConflictDetail(
                                attendee_email=conflict.attendee_email,
                                attendee_name=conflict.attendee_name,
                                conflicting_event_title=conflict.conflicting_event_title,
                                conflicting_start_at=conflict.conflicting_start_at,
                                conflicting_end_at=conflict.conflicting_end_at,
                            ).model_dump(mode="json")
                            for conflict in availability.conflicts
                        ],
                        "alternative_slots": [
                            {"start_at": start.isoformat(), "end_at": end.isoformat()}
                            for start, end in alternatives
                        ],
                    },
                )
    created = create_meeting(
        db=db,
        meeting_data=meeting,
        organizer_id=actor_id,
    )
    persisted_participants = (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.meeting_id == created.id)
        .all()
    )
    participant_emails = list(dict.fromkeys(
        participant.email.strip().casefold()
        for participant in persisted_participants
        if participant.email and participant.email.strip()
    ))
    participant_user_ids = {
        participant.user_id
        for participant in persisted_participants
        if participant.user_id
    }
    attendee_filters = []
    if participant_user_ids:
        attendee_filters.append(Employee.id.in_(participant_user_ids))
    if participant_emails:
        attendee_filters.append(
            func.lower(func.trim(Employee.email)).in_(participant_emails)
        )
    attendees = db.query(Employee).filter(or_(*attendee_filters)).all() if attendee_filters else []
    organizer = db.query(Employee).filter(Employee.id == actor_id).first()
    event = CalendarEvent(
        id=created.id,
        title=created.title,
        start_at=created.start_at,
        end_at=created.end_at,
        join_url=created.join_url,
        organizer_id=created.organizer_id,
        attendee_emails=participant_emails,
        status=created.status,
    )
    invitations_sent, invitation_failures = send_meeting_invitations(
        meeting=event,
        organizer_name=organizer.name if organizer else "Meeting Organizer",
        attendees=attendees,
        timezone_name=str(get_organization_timezone()),
        raw_emails=participant_emails,
    )
    return MeetingCreationResponse(
        id=created.id,
        title=created.title,
        agenda=created.agenda,
        start_at=_as_utc(created.start_at),
        end_at=_as_utc(created.end_at),
        join_url=created.join_url,
        status=created.status,
        organizer_id=created.organizer_id,
        timezone=str(get_organization_timezone()),
        invitations_sent=invitations_sent,
        invitation_failures=invitation_failures,
    )

@router.get("/calendar", response_model=MeetingCalendarResponse)
def get_calendar_meetings(
    year: int = Query(..., ge=2000, le=2200),
    month: int = Query(..., ge=1, le=12),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    calendar_timezone = get_organization_timezone()
    start_local = datetime(year, month, 1, tzinfo=calendar_timezone)
    next_month_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    end_local = datetime(next_month_year, next_month, 1, tzinfo=calendar_timezone)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    meetings = get_user_meetings(
        db=db,
        user_id=principal.user_id,
        email=principal.email,
        start_at=start_utc,
        end_at=end_utc,
    )
    meeting_ids = [meeting.id for meeting in meetings]
    participants = (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.meeting_id.in_(meeting_ids))
        .all()
        if meeting_ids
        else []
    )
    participant_ids = {
        participant.user_id
        for participant in participants
        if participant.user_id
    }
    participant_emails = {
        participant.email.lower()
        for participant in participants
        if participant.email
    }
    employee_filters = []
    if participant_ids:
        employee_filters.append(Employee.id.in_(participant_ids))
    if participant_emails:
        employee_filters.append(Employee.email.in_(participant_emails))
    employees = (
        db.query(Employee).filter(or_(*employee_filters)).all()
        if employee_filters
        else []
    )
    employees_by_id = {employee.id: employee for employee in employees}
    employees_by_email = {employee.email.lower(): employee for employee in employees}
    attendees_by_meeting: dict[str, dict[str, MeetingCalendarAttendeeResponse]] = {}
    for participant in participants:
        email_key = participant.email.lower() if participant.email else None
        employee = (
            employees_by_id.get(participant.user_id)
            if participant.user_id
            else employees_by_email.get(email_key) if email_key else None
        )
        attendee_email = employee.email if employee else participant.email
        if not attendee_email:
            continue
        attendee = MeetingCalendarAttendeeResponse(
            employee_id=employee.id if employee else participant.user_id,
            name=employee.name if employee else attendee_email,
            email=attendee_email,
        )
        attendees_by_meeting.setdefault(participant.meeting_id, {})[
            attendee_email.lower()
        ] = attendee

    def as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return MeetingCalendarResponse(
        timezone=str(calendar_timezone),
        meetings=[
            MeetingCalendarEventResponse(
                id=meeting.id,
                title=meeting.title,
                start_at=as_utc(meeting.start_at),
                end_at=as_utc(meeting.end_at),
                join_url=meeting.join_url,
                status=meeting.status,
                organizer_id=meeting.organizer_id,
                attendees=list(attendees_by_meeting.get(meeting.id, {}).values()),
            )
            for meeting in meetings
        ],
    )


@router.get("/", response_model=list[MeetingListResponse])
def get_meetings(
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
):
    meetings = get_user_meetings(
        db=db,
        user_id=principal.user_id,
        email=principal.email,
    )
    if principal.user_id:
        delegated_organizer_ids = [
            organizer_id
            for (organizer_id,) in db.query(MeetingDelegate.organizer_id)
            .filter(MeetingDelegate.delegate_id == principal.user_id)
            .all()
        ]
        delegated_meeting_ids = [
            meeting_id
            for (meeting_id,) in db.query(MeetingDelegate.meeting_id)
            .filter(
                MeetingDelegate.delegate_id == principal.user_id,
                MeetingDelegate.meeting_id.is_not(None),
            )
            .all()
        ]
        if delegated_organizer_ids or delegated_meeting_ids:
            delegated_meetings = (
                db.query(Meeting)
                .filter(
                    (Meeting.organizer_id.in_(delegated_organizer_ids))
                    | (Meeting.id.in_(delegated_meeting_ids))
                )
                .all()
            )
            known_ids = {meeting.id for meeting in meetings}
            meetings.extend(meeting for meeting in delegated_meetings if meeting.id not in known_ids)
    return [
        MeetingListResponse(
            id=m.id,
            title=m.title,
            agenda=m.agenda,
            start_at=_as_utc(m.start_at),
            end_at=_as_utc(m.end_at),
            join_url=m.join_url,
            status=m.status,
            organizer_id=m.organizer_id,
            timezone=str(get_organization_timezone()),
            can_manage=is_authorized_rescheduler(db, m, principal.user_id),
        )
        for m in meetings
    ]


@router.get("/employees", response_model=list[EmployeeResponse])
def get_active_employees(db: Session = Depends(get_db)):
    employees = (
        db.query(Employee)
        .filter(Employee.is_active.is_(True))
        .order_by(Employee.name.asc())
        .all()
    )
    return [
        EmployeeResponse(
            id=employee.id,
            name=employee.name,
            email=employee.email,
            team=employee.team,
        )
        for employee in employees
    ]


@router.post(
    "/schedule/natural-language",
    response_model=NaturalLanguageScheduleResponse,
    summary="Schedule a meeting using natural language (Story 5.1)",
)
def schedule_meeting_nl_endpoint(
    payload: NaturalLanguageScheduleRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    result = schedule_meeting_from_natural_language(
        db=db,
        prompt=payload.prompt,
        organizer_id=actor_id,
    )

    meeting_resp = None
    if result.meeting:
        meeting_resp = MeetingResponse(
            id=result.meeting.id,
            title=result.meeting.title,
            agenda=result.meeting.description,
            start_at=_as_utc(result.meeting.start_at),
            end_at=_as_utc(result.meeting.end_at),
            join_url=result.meeting.join_url or "",
            status=result.meeting.status,
            organizer_id=result.meeting.organizer_id,
            timezone=str(get_organization_timezone()),
        )

    conflicts = [
        ConflictDetail(
            attendee_email=c.attendee_email,
            conflicting_event_title=c.conflicting_event_title,
            conflicting_start_at=c.conflicting_start_at,
            conflicting_end_at=c.conflicting_end_at,
            attendee_name=c.attendee_name,
        )
        for c in result.conflicts
    ]
    alternative_slots = [
        AlternativeSlotDetail(start_at=start, end_at=end)
        for start, end in result.alternative_slots
    ]

    attendees = [
        EmployeeResponse(
            id=a["id"],
            name=a["name"],
            email=a["email"],
            team=a.get("team"),
        )
        for a in result.resolved_attendees
    ]

    return NaturalLanguageScheduleResponse(
        status=result.status,
        meeting=meeting_resp,
        timezone=str(get_organization_timezone()),
        clarifying_question=result.clarifying_question,
        conflicts=conflicts,
        alternative_slots=alternative_slots,
        message=result.message,
        resolved_attendees=attendees,
        invitations_sent=result.invitations_sent,
        invitation_failures=result.invitation_failures,
        calendar_provider=result.meeting.provider_name if result.meeting else "local",
        reschedule_request_id=result.reschedule_request_id,
    )


@router.post(
    "/schedule/confirm",
    response_model=NaturalLanguageScheduleResponse,
    summary="Confirm a proposed meeting time after rechecking availability",
)
def confirm_meeting_slot_endpoint(
    payload: ScheduleConfirmationRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    result = schedule_meeting_from_natural_language(
        db=db,
        prompt=payload.prompt,
        organizer_id=actor_id,
        selected_start_at=payload.start_at,
    )
    meeting_resp = None
    if result.meeting:
        meeting_resp = MeetingResponse(
            id=result.meeting.id,
            title=result.meeting.title,
            agenda=result.meeting.description,
            start_at=_as_utc(result.meeting.start_at),
            end_at=_as_utc(result.meeting.end_at),
            join_url=result.meeting.join_url or "",
            status=result.meeting.status,
            organizer_id=result.meeting.organizer_id,
            timezone=str(get_organization_timezone()),
        )
    return NaturalLanguageScheduleResponse(
        status=result.status,
        meeting=meeting_resp,
        timezone=str(get_organization_timezone()),
        clarifying_question=result.clarifying_question,
        conflicts=[
            ConflictDetail(
                attendee_email=conflict.attendee_email,
                attendee_name=conflict.attendee_name,
                conflicting_event_title=conflict.conflicting_event_title,
                conflicting_start_at=conflict.conflicting_start_at,
                conflicting_end_at=conflict.conflicting_end_at,
            )
            for conflict in result.conflicts
        ],
        alternative_slots=[
            AlternativeSlotDetail(start_at=start, end_at=end)
            for start, end in result.alternative_slots
        ],
        message=result.message,
        resolved_attendees=[
            EmployeeResponse(
                id=attendee["id"],
                name=attendee["name"],
                email=attendee["email"],
                team=attendee.get("team"),
            )
            for attendee in result.resolved_attendees
        ],
        invitations_sent=result.invitations_sent,
        invitation_failures=result.invitation_failures,
        calendar_provider=result.meeting.provider_name if result.meeting else "local",
        reschedule_request_id=result.reschedule_request_id,
    )


@router.post(
    "/reschedule/natural-language",
    response_model=NaturalLanguageScheduleResponse,
    summary="Reschedule a meeting using natural language (Story 5.3)",
)
def reschedule_meeting_nl_endpoint(
    payload: NaturalLanguageScheduleRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    result = reschedule_meeting_from_natural_language(
        db=db,
        prompt=payload.prompt,
        requester_id=actor_id,
    )

    meeting_resp = None
    if result.meeting:
        meeting_resp = MeetingResponse(
            id=result.meeting.id,
            title=result.meeting.title,
            agenda=result.meeting.description,
            start_at=_as_utc(result.meeting.start_at),
            end_at=_as_utc(result.meeting.end_at),
            join_url=result.meeting.join_url or "",
            status=result.meeting.status,
            organizer_id=result.meeting.organizer_id,
            timezone=str(get_organization_timezone()),
        )

    conflicts = [
        ConflictDetail(
            attendee_email=c.attendee_email,
            conflicting_event_title=c.conflicting_event_title,
            conflicting_start_at=c.conflicting_start_at,
            conflicting_end_at=c.conflicting_end_at,
            attendee_name=c.attendee_name,
        )
        for c in result.conflicts
    ]
    alternative_slots = [
        AlternativeSlotDetail(start_at=start, end_at=end)
        for start, end in result.alternative_slots
    ]

    attendees = [
        EmployeeResponse(
            id=a["id"],
            name=a["name"],
            email=a["email"],
            team=a.get("team"),
        )
        for a in result.resolved_attendees
    ]

    return NaturalLanguageScheduleResponse(
        status=result.status,
        meeting=meeting_resp,
        timezone=str(get_organization_timezone()),
        clarifying_question=result.clarifying_question,
        conflicts=conflicts,
        alternative_slots=alternative_slots,
        message=result.message,
        resolved_attendees=attendees,
        invitations_sent=result.invitations_sent,
        invitation_failures=result.invitation_failures,
        calendar_provider=result.meeting.provider_name if result.meeting else "local",
        reschedule_request_id=result.reschedule_request_id,
    )


@router.get(
    "/reschedule-requests",
    response_model=list[MeetingRescheduleRequestResponse],
    summary="List meeting reschedule requests (Story 5.3)",
)
def list_reschedule_requests_endpoint(
    status_filter: str | None = Query(default=None, alias="status"),
    meeting_id: str | None = Query(default=None),
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    query = db.query(MeetingRescheduleRequest)
    if meeting_id is not None:
        query = query.filter(MeetingRescheduleRequest.meeting_id == meeting_id)
    if status_filter:
        query = query.filter(MeetingRescheduleRequest.status == status_filter)

    delegate_org_ids = [
        organizer_id
        for (organizer_id,) in db.query(MeetingDelegate.organizer_id)
        .filter(MeetingDelegate.delegate_id == actor_id)
        .all()
    ]
    delegate_meeting_ids = [
        delegated_meeting_id
        for (delegated_meeting_id,) in db.query(MeetingDelegate.meeting_id)
        .filter(
            MeetingDelegate.delegate_id == actor_id,
            MeetingDelegate.meeting_id.is_not(None),
        )
        .all()
    ]
    query = query.join(Meeting, MeetingRescheduleRequest.meeting_id == Meeting.id).filter(
        (Meeting.organizer_id == actor_id)
        | (Meeting.organizer_id.in_(delegate_org_ids))
        | (Meeting.id.in_(delegate_meeting_ids))
        | (MeetingRescheduleRequest.requested_by == actor_id)
    )

    records = query.order_by(MeetingRescheduleRequest.created_at.desc()).all()
    results = []
    for r in records:
        meeting = db.query(Meeting).filter(Meeting.id == r.meeting_id).first()
        requester = db.query(Employee).filter(Employee.id == r.requested_by).first()
        part_count = (
            db.query(MeetingParticipant)
            .filter(MeetingParticipant.meeting_id == r.meeting_id)
            .count()
        )
        results.append(
            MeetingRescheduleRequestResponse(
                id=r.id,
                meeting_id=r.meeting_id,
                meeting_title=meeting.title if meeting else "Meeting",
                requested_by=r.requested_by,
                requested_by_name=requester.name if requester else None,
                new_start_at=_as_utc(r.new_start_at),
                new_end_at=_as_utc(r.new_end_at),
                reason=r.reason,
                status=r.status,
                decided_by=r.decided_by,
                decided_at=r.decided_at,
                decision_note=r.decision_note,
                created_at=r.created_at,
                attendee_count=part_count,
            )
        )
    return results


@router.post(
    "/reschedule-requests/{request_id}/decide",
    response_model=RescheduleDecisionResponse,
    summary="Approve or reject a meeting reschedule request (Story 5.3)",
)
def decide_reschedule_request_endpoint(
    request_id: str,
    payload: RescheduleDecisionRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    req, updated_event, message = decide_reschedule_request(
        db=db,
        request_id=request_id,
        decision=payload.decision,
        decider_id=actor_id,
        decision_note=payload.note,
    )
    meeting = db.query(Meeting).filter(Meeting.id == req.meeting_id).first()
    requester = db.query(Employee).filter(Employee.id == req.requested_by).first()
    part_count = (
        db.query(MeetingParticipant)
        .filter(MeetingParticipant.meeting_id == req.meeting_id)
        .count()
    )
    req_resp = MeetingRescheduleRequestResponse(
        id=req.id,
        meeting_id=req.meeting_id,
        meeting_title=meeting.title if meeting else "Meeting",
        requested_by=req.requested_by,
        requested_by_name=requester.name if requester else None,
        new_start_at=_as_utc(req.new_start_at),
        new_end_at=_as_utc(req.new_end_at),
        reason=req.reason,
        status=req.status,
        decided_by=req.decided_by,
        decided_at=req.decided_at,
        decision_note=req.decision_note,
        created_at=req.created_at,
        attendee_count=part_count,
    )
    meeting_resp = None
    if updated_event:
        meeting_resp = MeetingResponse(
            id=updated_event.id,
            title=updated_event.title,
            agenda=updated_event.description,
            start_at=_as_utc(updated_event.start_at),
            end_at=_as_utc(updated_event.end_at),
            join_url=updated_event.join_url or "",
            status=updated_event.status,
            organizer_id=updated_event.organizer_id,
            timezone=str(get_organization_timezone()),
        )
    return RescheduleDecisionResponse(
        request=req_resp,
        meeting=meeting_resp,
        message=message,
    )


@router.post(
    "/delegates",
    response_model=MeetingDelegateResponse,
    summary="Authorize a delegate for meeting scheduling and rescheduling (Story 5.3)",
)
def add_meeting_delegate_endpoint(
    payload: MeetingDelegateCreateRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    delegate_record = add_meeting_delegate(
        db=db,
        organizer_id=actor_id,
        delegate_id=payload.delegate_id,
        meeting_id=payload.meeting_id,
    )
    return MeetingDelegateResponse(
        id=delegate_record.id,
        organizer_id=delegate_record.organizer_id,
        delegate_id=delegate_record.delegate_id,
        meeting_id=delegate_record.meeting_id,
        created_at=delegate_record.created_at,
    )


@router.get(
    "/delegates",
    response_model=list[MeetingDelegateResponse],
    summary="List authorized meeting delegates (Story 5.3)",
)
def list_meeting_delegates_endpoint(
    actor_id: str = Depends(require_authenticated_actor_id),
    meeting_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    delegates = get_meeting_delegates(
        db=db,
        organizer_id=actor_id,
        meeting_id=meeting_id,
    )
    return [
        MeetingDelegateResponse(
            id=d.id,
            organizer_id=d.organizer_id,
            delegate_id=d.delegate_id,
            meeting_id=d.meeting_id,
            created_at=d.created_at,
        )
        for d in delegates
    ]



@router.get("/{meeting_id}/validate", response_model=MeetingValidationResponse)
def validate_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.status in TERMINAL_STATUSES:
        # Completed meetings return 410 so callers know they cannot join
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This meeting has already completed",
        )

    if is_meeting_expired(meeting):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Meeting has expired",
        )

    return MeetingValidationResponse(
        id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=meeting.start_at,
        end_at=meeting.end_at,
        status=meeting.status,
        organizer_id=meeting.organizer_id,
        is_valid=True,
        is_expired=False,
    )


@router.delete(
    "/{meeting_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Organizer deletes a completed meeting",
)
def delete_meeting_route(
    meeting_id: str,
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    """Permanently delete a completed meeting and its attendance records.
    Only the organizer may call this, and only on completed/cancelled meetings.
    """
    delete_meeting(db=db, meeting_id=meeting_id, organizer_id=actor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{meeting_id}/cancel",
    response_model=MeetingChangeResponse,
    summary="Cancel a meeting or submit a high-attendance cancellation for approval (Story 5.4)",
)
def cancel_meeting_route(
    meeting_id: str,
    payload: MeetingCancellationRequestCreate | None = None,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting, request, sent, failures = request_meeting_cancellation(
        db=db,
        meeting_id=meeting_id,
        actor_id=actor_id,
        reason=payload.reason if payload else None,
    )
    pending = request is not None
    return MeetingChangeResponse(
        status="pending_approval" if pending else "applied",
        message=(
            "Cancellation submitted for organizer/delegate approval."
            if pending
            else "Meeting cancelled."
        ),
        meeting=_meeting_response(meeting),
        request_id=request.id if request else None,
        notifications_sent=sent,
        notification_failures=failures,
    )


@router.get(
    "/cancellation-requests",
    response_model=list[MeetingCancellationRequestResponse],
    summary="List pending meeting cancellation requests visible to the organizer or delegate",
)
def list_cancellation_requests_route(
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    return [
        MeetingCancellationRequestResponse(
            id=request.id,
            meeting_id=meeting.id,
            meeting_title=meeting.title,
            requested_by=request.requested_by,
            reason=request.reason,
            status=request.status,
            decided_by=request.decided_by,
            decided_at=request.decided_at,
            decision_note=request.decision_note,
            created_at=request.created_at,
            attendee_count=count,
        )
        for request, meeting, count in list_pending_cancellations(db, actor_id)
    ]


@router.post(
    "/cancellation-requests/{request_id}/decide",
    response_model=MeetingCancellationDecisionResponse,
    summary="Approve or reject a high-attendance meeting cancellation (Story 5.4)",
)
def decide_cancellation_route(
    request_id: str,
    payload: MeetingCancellationDecisionRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    request, meeting, sent, failures = decide_meeting_cancellation(
        db=db,
        request_id=request_id,
        decision=payload.decision,
        actor_id=actor_id,
        note=payload.note,
    )
    meeting_record = db.query(Meeting).filter(Meeting.id == request.meeting_id).first()
    return MeetingCancellationDecisionResponse(
        request=MeetingCancellationRequestResponse(
            id=request.id,
            meeting_id=request.meeting_id,
            meeting_title=meeting_record.title if meeting_record else "Meeting",
            requested_by=request.requested_by,
            reason=request.reason,
            status=request.status,
            decided_by=request.decided_by,
            decided_at=request.decided_at,
            decision_note=request.decision_note,
            created_at=request.created_at,
            attendee_count=attendee_count(db, meeting_record) if meeting_record else 0,
        ),
        meeting=_meeting_response(meeting) if meeting else None,
        message=(
            "Cancellation approved and meeting cancelled."
            if request.status == "approved"
            else "Cancellation request rejected."
        ),
        notifications_sent=sent,
        notification_failures=failures,
    )


@router.post(
    "/{meeting_id}/participants/bulk",
    response_model=MeetingParticipantBulkChangeResponse,
    summary="Invite multiple participants to a meeting (Story 5.4)",
)
def add_meeting_participants_route(
    meeting_id: str,
    payload: MeetingParticipantBulkChangeRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    _, outcomes = add_meeting_participants(db, meeting_id, actor_id, payload)
    added_count = len(outcomes["added"])
    skipped_count = (
        len(outcomes["already_present"])
        + len(outcomes["duplicate_addresses"])
    )
    failed_count = (
        len(outcomes["invalid_addresses"])
        + len(outcomes["failed_addresses"])
        + len(outcomes["notification_failures"])
    )
    message = (
        f"{added_count} participant{'s' if added_count != 1 else ''} added; "
        f"{failed_count} address{'es' if failed_count != 1 else ''} "
        f"{'was' if failed_count == 1 else 'were'} invalid or failed."
    )
    if skipped_count:
        message += f" {skipped_count} duplicate or already invited address{'es' if skipped_count != 1 else ''} skipped."
    return MeetingParticipantBulkChangeResponse(message=message, **outcomes)


@router.post(
    "/{meeting_id}/participants",
    response_model=MeetingChangeResponse,
    summary="Invite a participant to a meeting (Story 5.4)",
)
def add_meeting_participant_route(
    meeting_id: str,
    payload: MeetingParticipantChangeRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting, sent, failures = add_meeting_participant(db, meeting_id, actor_id, payload)
    return MeetingChangeResponse(
        status="applied",
        message="Participant added.",
        meeting=_meeting_response(meeting),
        notifications_sent=sent,
        notification_failures=failures,
    )


@router.delete(
    "/{meeting_id}/participants/{participant_id}",
    response_model=MeetingChangeResponse,
    summary="Remove a participant from a meeting (Story 5.4)",
)
def remove_meeting_participant_route(
    meeting_id: str,
    participant_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting, sent, failures = remove_meeting_participant(db, meeting_id, participant_id, actor_id)
    return MeetingChangeResponse(
        status="applied",
        message="Participant removed.",
        meeting=_meeting_response(meeting),
        notifications_sent=sent,
        notification_failures=failures,
    )


@router.get(
    "/{meeting_id}/participants",
    response_model=list[MeetingParticipantResponse],
    summary="List participants for an authorized meeting manager (Story 5.4)",
)
def list_meeting_participants_route(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    participants = get_meeting_participants(db, meeting_id, actor_id)
    return [
        MeetingParticipantResponse(
            id=participant.id,
            user_id=participant.user_id,
            name=employee.name if employee else participant.email or "Guest",
            email=participant.email or (employee.email if employee else None),
            role=participant.role,
        )
        for participant, employee in participants
    ]


@router.get(
    "/{meeting_id}/audit",
    response_model=list[MeetingAuditLogResponse],
    summary="Read meeting change audit history (Story 5.4)",
)
def meeting_audit_route(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    return [
        MeetingAuditLogResponse(
            id=entry.id,
            meeting_id=entry.meeting_id,
            actor_id=entry.actor_id,
            action=entry.action,
            details=entry.details,
            created_at=entry.created_at,
        )
        for entry in list_meeting_audit(db, meeting_id, actor_id)
    ]


@router.post(
    "/{meeting_id}/end",
    response_model=MeetingResponse,
    summary="Organizer forcibly ends the meeting for everyone",
)
async def end_meeting_for_everyone(
    meeting_id: str,
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    """End the meeting immediately, stamping all attendance records and
    broadcasting a 'meeting_ended' signal to all connected participants.
    """
    meeting = force_complete_meeting(
        db=db,
        meeting_id=meeting_id,
        organizer_id=actor_id,
    )
    # Broadcast to all connected WebSocket participants
    await signaling_manager.broadcast(
        meeting_id=meeting_id,
        message={
            "type": "meeting_ended",
            "sender_id": "server",
            "payload": {
                "meeting_id": meeting_id,
                "reason": "The host has ended the meeting.",
            },
        },
    )
    # Close participant WebSocket connections
    await signaling_manager.close_room(
        meeting_id=meeting_id,
        code=1000,
        reason="Meeting ended by host",
    )
    return MeetingResponse(
        id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=_as_utc(meeting.start_at),
        end_at=_as_utc(meeting.end_at),
        join_url=meeting.join_url,
        status=meeting.status,
        organizer_id=meeting.organizer_id,
        timezone=str(get_organization_timezone()),
    )


@router.get("/{meeting_id}", response_model=MeetingValidationResponse)
def get_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    return validate_meeting(meeting_id=meeting_id, db=db)


@router.post(
    "/{meeting_id}/attendance/join",
    response_model=AttendanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def join_meeting_attendance(
    meeting_id: str,
    participant_data: AttendanceJoinRequest,
    db: Session = Depends(get_db),
):
    return record_participant_join(
        db=db,
        meeting_id=meeting_id,
        participant_data=participant_data,
    )


@router.post(
    "/{meeting_id}/attendance/{attendance_id}/leave",
    response_model=AttendanceResponse,
    status_code=status.HTTP_200_OK,
)
def leave_meeting_attendance(
    meeting_id: str,
    attendance_id: str,
    db: Session = Depends(get_db),
):
    return record_participant_leave(
        db=db,
        meeting_id=meeting_id,
        attendance_id=attendance_id,
    )


@router.post(
    "/{meeting_id}/recording/consent",
    response_model=RecordingConsentResponse,
    summary="Record a participant's recording consent decision",
)
def recording_consent(
    meeting_id: str,
    consent_data: RecordingConsentRequest,
    db: Session = Depends(get_db),
):
    consent = record_meeting_recording_consent(
        db=db,
        meeting_id=meeting_id,
        attendance_id=consent_data.attendance_id,
        consent_status=consent_data.consent_status,
    )
    return RecordingConsentResponse(
        id=consent.id,
        meeting_id=consent.meeting_id,
        attendance_id=consent.attendance_id,
        consent_status=consent.consent_status,
        consented_at=consent.consented_at,
        revoked_at=consent.revoked_at,
        created_at=consent.created_at,
    )


def _recording_response(recording) -> MeetingRecordingResponse:
    return MeetingRecordingResponse(
        id=recording.id,
        meeting_id=recording.meeting_id,
        participant_id=recording.participant_id,
        status=recording.status,
        started_at=recording.started_at,
        completed_at=recording.completed_at,
        created_at=recording.created_at,
        retention_expires_at=recording.retention_expires_at,
        storage_uri=recording.storage_uri,
        error_message=recording.error_message,
    )


@router.get(
    "/{meeting_id}/recording",
    response_model=MeetingRecordingResponse,
    summary="Get the latest recording state",
)
def read_meeting_recording(
    meeting_id: str,
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    recording = get_meeting_recording(db, meeting_id, actor_id)
    return _recording_response(recording)


@router.post(
    "/{meeting_id}/recording/start",
    response_model=MeetingRecordingResponse,
    summary="Start recording state after all active participants consent",
)
def begin_meeting_recording(
    meeting_id: str,
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    recording = start_meeting_recording(db, meeting_id, actor_id)
    return _recording_response(recording)


@router.post(
    "/{meeting_id}/recording/stop",
    response_model=MeetingRecordingResponse,
    summary="Stop active recording state",
)
def end_meeting_recording(
    meeting_id: str,
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    recording = stop_meeting_recording(db, meeting_id, actor_id)
    return _recording_response(recording)


@router.post(
    "/{meeting_id}/recording/upload",
    response_model=MeetingRecordingUploadResponse,
    summary="Persist a completed meeting recording file",
)
def upload_meeting_recording_file(
    meeting_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    result = upload_meeting_recording(
        db=db,
        meeting_id=meeting_id,
        organizer_id=actor_id,
        upload=file.file,
        filename=file.filename,
        content_type=file.content_type,
    )
    return MeetingRecordingUploadResponse(**result)


@router.post(
    "/{meeting_id}/recording/tracks",
    response_model=list[ParticipantTrackRegisterResponse],
    status_code=200,
    summary="Register participant-track recording rows (FR-8)",
)
def register_participant_tracks(
    meeting_id: str,
    body: ParticipantTracksRegisterRequest,
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    """Creates one MeetingRecording row (status=processing) per submitted track.

    The frontend calls this after stopping the main recording and before
    uploading individual participant track blobs.  No MeetingTranscript rows
    are created here; they are created by the per-track upload endpoint.
    """
    rows = register_participant_track_recordings(
        db=db,
        meeting_id=meeting_id,
        organizer_id=actor_id,
        tracks=[
            {
                "connection_id": track.connection_id,
                "participant_name": track.participant_name,
                "attendance_id": track.attendance_id,
            }
            for track in body.tracks
        ],
    )
    return [
        ParticipantTrackRegisterResponse(
            recording_id=row.id,
            connection_id=row.connection_id,
            participant_name=row.participant_name,
            status=row.status,
            meeting_id=row.meeting_id,
        )
        for row in rows
    ]


@router.post(
    "/{meeting_id}/recording/{recording_id}/upload",
    response_model=MeetingRecordingUploadResponse,
    summary="Upload a participant-specific audio track by explicit recording ID (FR-8)",
)
def upload_participant_track_file(
    meeting_id: str,
    recording_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor_id: str = Depends(require_authenticated_actor_id),
):
    """Upload and encrypt a single participant audio track.

    The recording_id must have been previously registered via
    POST /recording/tracks and must belong to this meeting.
    A MeetingTranscript row is created automatically upon success.
    """
    result = upload_participant_track_recording(
        db=db,
        meeting_id=meeting_id,
        recording_id=recording_id,
        organizer_id=actor_id,
        upload=file.file,
        filename=file.filename,
        content_type=file.content_type,
    )
    return MeetingRecordingUploadResponse(**result)


# -- Meeting report -----------------------------------------------------------


@router.get(
    "/{meeting_id}/report",
    response_model=MeetingReportResponse,
    summary="Organizer or assigned employee retrieves meeting source context",
)
def get_meeting_report(
    meeting_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Return a meeting report to its organizer or an assigned task employee.

    Assigned employees receive meeting details without the organizer-only
    attendance list. Identity is supplied by the current demo authentication
    seam; production deployments must replace it with a verified session.
    """
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    is_organizer = meeting.organizer_id == principal.user_id
    is_assigned_task_viewer = _is_assigned_task_viewer(
        db,
        meeting_id,
        principal.email,
    )
    if not is_organizer and not is_assigned_task_viewer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer or an assigned task employee can access the report",
        )

    logs = []
    if is_organizer:
        logs = (
            db.query(MeetingAttendanceLog)
            .filter(MeetingAttendanceLog.meeting_id == meeting_id)
            .order_by(MeetingAttendanceLog.joined_at)
            .all()
        )

    return MeetingReportResponse(
        meeting_id=meeting.id,
        title=meeting.title,
        agenda=meeting.agenda,
        start_at=meeting.start_at,
        end_at=meeting.end_at,
        organizer_id=meeting.organizer_id,
        attendance=[
            AttendanceReportRow(
                participant_name=log.participant_name,
                participant_email=log.participant_email,
                user_id=log.user_id,
                is_external_guest=log.is_external_guest,
                joined_at=log.joined_at,
                left_at=log.left_at,
            )
            for log in logs
        ],
    )


# -- Guest Lobby REST Endpoints ------------------------------------------------


@router.post(
    "/{meeting_id}/lobby",
    response_model=GuestRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="External guest requests to join meeting lobby",
)
def guest_join_lobby(
    meeting_id: str,
    guest_data: GuestJoinRequest,
    db: Session = Depends(get_db),
):
    """An external guest submits their name/email and enters the lobby.
    Returns a lobby request record with ``status='pending'``.
    The organizer is notified via WebSocket.
    """
    guest_req = request_guest_join(db=db, meeting_id=meeting_id, guest_data=guest_data)
    return GuestRequestResponse(
        id=guest_req.id,
        meeting_id=guest_req.meeting_id,
        guest_name=guest_req.guest_name,
        guest_email=guest_req.guest_email,
        status=guest_req.status,
        requested_at=guest_req.requested_at,
        decided_at=guest_req.decided_at,
        attendance_log_id=guest_req.attendance_log_id,
    )


@router.get(
    "/{meeting_id}/lobby",
    response_model=list[GuestRequestResponse],
    summary="Organizer lists pending lobby requests",
)
def list_pending_guests(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    """Returns all pending guest requests for the given meeting."""
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    if not is_authorized_rescheduler(db, meeting, actor_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Meeting manager access required")
    pending = get_pending_guest_requests(db=db, meeting_id=meeting_id)
    return [
        GuestRequestResponse(
            id=r.id,
            meeting_id=r.meeting_id,
            guest_name=r.guest_name,
            guest_email=r.guest_email,
            status=r.status,
            requested_at=r.requested_at,
            decided_at=r.decided_at,
            attendance_log_id=r.attendance_log_id,
        )
        for r in pending
    ]


@router.post(
    "/{meeting_id}/lobby/{request_id}/decision",
    response_model=GuestRequestResponse,
    summary="Organizer admits or rejects a guest lobby request",
)
async def organizer_decide_guest(
    meeting_id: str,
    request_id: str,
    decision: GuestDecisionRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    """Decide a guest request and notify its connected lobby socket."""
    guest_req = decide_guest_request(
        db=db,
        meeting_id=meeting_id,
        request_id=request_id,
        action=decision.decision,
        organizer_id=actor_id,
    )
    await signaling_manager.send_personal_message(
        meeting_id=f"lobby:{meeting_id}",
        target_id=guest_req.id,
        message={
            "type": (
                "guest_admitted"
                if guest_req.status == "admitted"
                else "guest_rejected"
            ),
            "guest_request_id": guest_req.id,
            "meeting_id": guest_req.meeting_id,
            "guest_name": guest_req.guest_name,
            "status": guest_req.status,
            "decided_at": guest_req.decided_at.isoformat()
            if guest_req.decided_at
            else None,
        },
    )
    return GuestRequestResponse(
        id=guest_req.id,
        meeting_id=guest_req.meeting_id,
        guest_name=guest_req.guest_name,
        guest_email=guest_req.guest_email,
        status=guest_req.status,
        requested_at=guest_req.requested_at,
        decided_at=guest_req.decided_at,
        attendance_log_id=guest_req.attendance_log_id,
    )


@router.websocket("/{meeting_id}/ws")
async def meeting_websocket_signaling(
    websocket: WebSocket,
    meeting_id: str,
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting not found",
        )
        return

    if meeting.status in TERMINAL_STATUSES:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting has already completed",
        )
        return

    if is_meeting_expired(meeting):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting has expired",
        )
        return

    authenticated_principal: AuthenticatedPrincipal | None = None
    authorization = websocket.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid authentication credentials",
            )
            return
        try:
            authenticated_principal = authenticate_access_token(db, token.strip())
        except AuthConfigurationError:
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="Authentication is not configured",
            )
            return
        except InvalidAccessToken:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid or expired access token",
            )
            return

    # Release initial read transaction so SQLite does not hold a read lock during the WS loop
    db.commit()

    connection_id = await signaling_manager.connect(
        meeting_id=meeting_id,
        websocket=websocket,
    )

    existing_participants = signaling_manager.get_room_participants(
        meeting_id=meeting_id,
        exclude_id=connection_id,
    )

    # Send welcome message with session ID and active peers
    await websocket.send_json({
        "type": "welcome",
        "sender_id": "server",
        "target_id": connection_id,
        "payload": {
            "connection_id": connection_id,
            "meeting_id": meeting_id,
            "participants": existing_participants,
            "organizer_id": meeting.organizer_id,
        },
    })

    # Broadcast participant_joined to existing room peers
    await signaling_manager.broadcast(
        meeting_id=meeting_id,
        message={
            "type": "participant_joined",
            "sender_id": connection_id,
            "payload": {
                "connection_id": connection_id,
            },
        },
        exclude_id=connection_id,
    )

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                message = json.loads(raw_text)
            except Exception:
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": "Invalid message: must be valid JSON",
                    },
                })
                continue

            if not isinstance(message, dict):
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": "Invalid message: root must be a JSON object",
                    },
                })
                continue

            msg_type = message.get("type")
            if not msg_type or not isinstance(msg_type, str):
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": "Invalid message: missing or invalid 'type' field",
                    },
                })
                continue

            if msg_type in ["offer", "answer", "ice_candidate"]:
                target_id = message.get("target_id")
                if not target_id or not isinstance(target_id, str):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"Missing or invalid 'target_id' for '{msg_type}' message",
                        },
                    })
                    continue

                if not signaling_manager.is_participant_in_meeting(meeting_id, target_id):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"Target participant '{target_id}' not found in meeting",
                        },
                    })
                    continue

                # Forward signaling payload to intended target peer
                await signaling_manager.send_personal_message(
                    meeting_id=meeting_id,
                    target_id=target_id,
                    message={
                        "type": msg_type,
                        "sender_id": connection_id,
                        "target_id": target_id,
                        "payload": message.get("payload", {}),
                    },
                )

            elif msg_type == "join":
                await websocket.send_json({
                    "type": "join_ack",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "connection_id": connection_id,
                        "meeting_id": meeting_id,
                    },
                })

            elif msg_type == "guest_request_notify":
                # Organizer-to-guest or guest-to-organizer lobby notification.
                # Simply broadcast to all room participants (organizer is in the room).
                await signaling_manager.broadcast(
                    meeting_id=meeting_id,
                    message={
                        "type": "guest_request_notify",
                        "sender_id": connection_id,
                        "payload": message.get("payload", {}),
                    },
                    exclude_id=connection_id,
                )

            elif msg_type in ("mute_participant", "ask_unmute"):
                # Only a token-authenticated organizer may control another participant.
                payload = message.get("payload", {})
                target_participant_id = payload.get("participant_id") if isinstance(payload, dict) else None

                # Reload meeting to get authoritative organizer_id
                current_meeting = get_meeting_by_id(db, meeting_id)
                if not current_meeting:
                    continue  # meeting disappeared

                is_organizer = bool(
                    authenticated_principal
                    and current_meeting.organizer_id == authenticated_principal.user_id
                )

                if not is_organizer:
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": "Only the meeting organizer can mute participants",
                        },
                    })
                    continue

                if not target_participant_id or not isinstance(target_participant_id, str):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"{msg_type} requires a participant_id in payload",
                        },
                    })
                    continue

                if not signaling_manager.is_participant_in_meeting(meeting_id, target_participant_id):
                    await websocket.send_json({
                        "type": "error",
                        "sender_id": "server",
                        "target_id": connection_id,
                        "payload": {
                            "detail": f"Target participant '{target_participant_id}' not found in meeting",
                        },
                    })
                    continue

                if msg_type == "mute_participant":
                    # Forward hard-mute command only to the targeted participant
                    await signaling_manager.send_personal_message(
                        meeting_id=meeting_id,
                        target_id=target_participant_id,
                        message={
                            "type": "mute_participant",
                            "sender_id": "server",
                            "target_id": target_participant_id,
                            "payload": {
                                "participant_id": target_participant_id,
                                "by_organizer": True,
                                "meeting_id": meeting_id,
                            },
                        },
                    )
                else:  # ask_unmute
                    # Forward a polite unmute request; the participant decides whether to unmute
                    await signaling_manager.send_personal_message(
                        meeting_id=meeting_id,
                        target_id=target_participant_id,
                        message={
                            "type": "ask_unmute",
                            "sender_id": "server",
                            "target_id": target_participant_id,
                            "payload": {
                                "participant_id": target_participant_id,
                                "meeting_id": meeting_id,
                            },
                        },
                    )

            elif msg_type == "participant_state":
                # Identity and organizer state are server-derived; ignore client assertions.
                payload = message.get("payload")
                state_payload = dict(payload) if isinstance(payload, dict) else {}
                state_payload["connection_id"] = connection_id
                current_meeting = get_meeting_by_id(db, meeting_id)
                state_payload["is_organizer"] = bool(
                    authenticated_principal
                    and current_meeting
                    and current_meeting.organizer_id == authenticated_principal.user_id
                )
                await signaling_manager.broadcast(
                    meeting_id=meeting_id,
                    message={
                        "type": "participant_state",
                        "sender_id": connection_id,
                        "payload": state_payload,
                    },
                    exclude_id=connection_id,
                )

            elif msg_type == "leave":
                break

            else:
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "target_id": connection_id,
                    "payload": {
                        "detail": f"Unsupported message type: '{msg_type}'",
                    },
                })

    except WebSocketDisconnect:
        pass
    finally:
        signaling_manager.disconnect(meeting_id, connection_id)
        await signaling_manager.broadcast(
            meeting_id=meeting_id,
            message={
                "type": "participant_left",
                "sender_id": connection_id,
                "payload": {
                    "connection_id": connection_id,
                },
            },
            exclude_id=connection_id,
        )
        # Auto-complete the meeting when the last participant has left
        _try_complete_meeting(db, meeting_id)


# -- Lobby-only WebSocket (for guests waiting before admission) ----------------


@router.websocket("/{meeting_id}/lobby-ws")
async def lobby_websocket(
    websocket: WebSocket,
    meeting_id: str,
    guest_request_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Dedicated WebSocket channel bound to a validated guest request."""
    meeting = get_meeting_by_id(db, meeting_id)
    if not meeting:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting not found",
        )
        return

    if is_meeting_expired(meeting):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Meeting has expired",
        )
        return

    if not guest_request_id:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request ID is required",
        )
        return

    guest_request = get_guest_request_by_id(db, guest_request_id)
    if not guest_request or guest_request.meeting_id != meeting_id:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request not found for this meeting",
        )
        return

    if guest_request.status not in ("pending", "admitted", "rejected"):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Guest request is not valid",
        )
        return

    # Use a lobby-namespaced room key so lobby connections are isolated
    lobby_room_id = f"lobby:{meeting_id}"
    # If this guest request already has an active lobby connection (e.g. from page refresh,
    # network reconnect, or React StrictMode re-mount), cleanly disconnect/replace the
    # previous stale connection so the valid guest can reconnect without getting rejected.
    if signaling_manager.is_participant_in_meeting(lobby_room_id, guest_request_id):
        existing_ws = signaling_manager.rooms.get(lobby_room_id, {}).get(guest_request_id)
        if existing_ws:
            try:
                await existing_ws.close(
                    code=status.WS_1000_NORMAL_CLOSURE,
                    reason="Replaced by new lobby connection",
                )
            except Exception:
                pass
        signaling_manager.disconnect(lobby_room_id, guest_request_id)

    connection_id = await signaling_manager.connect(
        meeting_id=lobby_room_id,
        websocket=websocket,
        connection_id=guest_request_id,
    )

    if guest_request.status in ("admitted", "rejected"):
        await signaling_manager.send_personal_message(
            meeting_id=lobby_room_id,
            target_id=connection_id,
            message={
                "type": (
                    "guest_admitted"
                    if guest_request.status == "admitted"
                    else "guest_rejected"
                ),
                "guest_request_id": guest_request.id,
                "meeting_id": guest_request.meeting_id,
                "guest_name": guest_request.guest_name,
                "status": guest_request.status,
                "decided_at": guest_request.decided_at.isoformat()
                if guest_request.decided_at
                else None,
            },
        )
        signaling_manager.disconnect(lobby_room_id, connection_id)
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        return

    await websocket.send_json({
        "type": "lobby_welcome",
        "sender_id": "server",
        "payload": {
            "connection_id": connection_id,
            "meeting_id": meeting_id,
            "message": "You are in the lobby. Waiting for the organizer to admit you.",
        },
    })

    # Notify the main meeting room that a new guest has arrived
    await signaling_manager.broadcast(
        meeting_id=meeting_id,
        message={
            "type": "guest_arrived",
            "sender_id": "server",
            "payload": {
                "guest_request_id": connection_id,
                "meeting_id": meeting_id,
            },
        },
    )

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                message = json.loads(raw_text)
            except Exception:
                await websocket.send_json({
                    "type": "error",
                    "sender_id": "server",
                    "payload": {"detail": "Invalid JSON"},
                })
                continue

            msg_type = message.get("type", "")
            if msg_type == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "sender_id": "server",
                })
            elif msg_type == "leave":
                break

    except WebSocketDisconnect:
        pass
    finally:
        signaling_manager.disconnect(lobby_room_id, connection_id)


@router.get(
        "/{meeting_id}/summary",
        response_model=MeetingSummaryResponse,
        summary="Organizer retrieves the persisted AI meeting summary",
)
def get_meeting_summary(
        meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
        db: Session = Depends(get_db),
):
        meeting = get_meeting_by_id(db, meeting_id)
        if meeting is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Meeting not found",
            )

        if meeting.organizer_id != actor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the meeting organizer can access the summary",
            )

        summary = (
            db.query(MeetingSummary)
            .filter(MeetingSummary.meeting_id == meeting_id)
            .order_by(MeetingSummary.created_at.desc(), MeetingSummary.id.desc())
            .first()
        )
        if summary is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Meeting summary not found",
            )

        return MeetingSummaryResponse.model_validate(summary)


def _single_action_item_response(
    action_item: MeetingActionItem,
    meeting_id: str,
    transcript_id: str,
) -> MeetingActionItemResponse:
    threshold = get_action_item_confidence_threshold()
    review_status = (
        action_item.review_status or ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW
    )
    requires_approval = (
        review_status == ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED
        or (
            review_status == ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW
            and action_item.confidence < threshold
        )
    )
    is_task_eligible = review_status == ACTION_ITEM_REVIEW_STATUS_CONFIRMED

    return MeetingActionItemResponse(
        id=action_item.id,
        owner_name=action_item.owner_name,
        proposed_employee_id=action_item.proposed_employee_id,
        confirmed_employee_id=action_item.confirmed_employee_id,
        employee_match_status=action_item.employee_match_status,
        ownership_status=action_item.ownership_status,
        review_status=review_status,
        reviewed_by=action_item.reviewed_by,
        reviewed_at=action_item.reviewed_at,
        is_task_eligible=is_task_eligible,
        requires_approval=requires_approval,
        task_description=action_item.task_description,
        deadline_at=_as_utc(action_item.deadline_at),
        deadline_text=action_item.deadline_text,
        priority=action_item.priority,
        confidence=action_item.confidence,
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        source_segment_index=action_item.source_segment_index,
        source_start_seconds=action_item.source_start_seconds,
        source_end_seconds=action_item.source_end_seconds,
        source_quote_start_seconds=action_item.source_quote_start_seconds,
        source_quote_end_seconds=action_item.source_quote_end_seconds,
        source_quote=action_item.source_quote,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _meeting_action_items_response(
    extraction: MeetingActionItemExtraction,
) -> MeetingActionItemsResponse:
    return MeetingActionItemsResponse(
        id=extraction.id,
        meeting_id=extraction.meeting_id,
        transcript_id=extraction.transcript_id,
        status=extraction.status,
        created_at=extraction.created_at,
        updated_at=extraction.updated_at,
        processing_started_at=extraction.processing_started_at,
        completed_at=extraction.completed_at,
        error_message=extraction.error_message,
        action_items=[
            _single_action_item_response(
                item,
                extraction.meeting_id,
                extraction.transcript_id,
            )
            for item in sorted(
                extraction.items,
                key=lambda action_item: (
                    action_item.source_segment_index,
                    action_item.id,
                ),
            )
        ],
    )


@router.post(
    "/{meeting_id}/action-items/extract",
    response_model=MeetingActionItemsResponse,
    summary="Extract action items from the completed meeting transcript",
)
def extract_meeting_action_items(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    retry_empty: bool = Query(
        default=False,
        description="Re-run an extraction that completed without finding any action items.",
    ),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can extract action items",
        )

    transcript = get_meeting_transcript(db, meeting_id)
    if transcript is None or transcript.status != TRANSCRIPT_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Completed transcript not found",
        )

    try:
        extraction = (
            generate_meeting_action_items(db, transcript.id, retry_empty=True)
            if retry_empty
            else generate_meeting_action_items(db, transcript.id)
        )
    except MeetingActionItemExtractionError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Action item extraction failed",
        ) from None

    synchronize_confirmed_action_item_tasks(db, extraction, actor_id)
    return _meeting_action_items_response(extraction)


@router.post(
    "/{meeting_id}/action-items/{action_item_id}/confirm-owner",
    response_model=MeetingActionItemResponse,
    summary="Confirm or select the responsible employee for an extracted action item",
)
def confirm_action_item_owner(
    meeting_id: str,
    action_item_id: str,
    payload: ActionItemOwnerConfirmationRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can confirm ownership",
        )

    action_item = (
        db.query(MeetingActionItem)
        .join(
            MeetingActionItemExtraction,
            MeetingActionItem.extraction_id == MeetingActionItemExtraction.id,
        )
        .filter(
            MeetingActionItem.id == action_item_id,
            MeetingActionItemExtraction.meeting_id == meeting_id,
        )
        .one_or_none()
    )
    if action_item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action item not found for this meeting",
        )

    employee = db.query(Employee).filter(Employee.id == payload.employee_id).one_or_none()
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Employee not found",
        )

    if not employee.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Employee is inactive",
        )

    if action_item.review_status == ACTION_ITEM_REVIEW_STATUS_REJECTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rejected action items cannot be changed",
        )

    action_item.ownership_status = MEETING_ACTION_ITEM_OWNERSHIP_STATUS_CONFIRMED
    action_item.confirmed_employee_id = employee.id
    db.commit()
    db.refresh(action_item)

    return _single_action_item_response(
        action_item,
        meeting_id,
        action_item.extraction.transcript_id,
    )


@router.patch(
    "/{meeting_id}/action-items/{action_item_id}/review",
    response_model=MeetingActionItemResponse,
    summary="Review an extracted action item (confirm, edit, or reject)",
)
@router.post(
    "/{meeting_id}/action-items/{action_item_id}/review",
    response_model=MeetingActionItemResponse,
    summary="Review an extracted action item (confirm, edit, or reject)",
)
def review_action_item_endpoint(
    meeting_id: str,
    action_item_id: str,
    payload: ActionItemReviewRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can review action items",
        )

    action_item = (
        db.query(MeetingActionItem)
        .join(
            MeetingActionItemExtraction,
            MeetingActionItem.extraction_id == MeetingActionItemExtraction.id,
        )
        .filter(
            MeetingActionItem.id == action_item_id,
            MeetingActionItemExtraction.meeting_id == meeting_id,
        )
        .one_or_none()
    )
    if action_item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action item not found for this meeting",
        )

    try:
        updated_item = review_action_item(
            db=db,
            meeting_id=meeting_id,
            action_item_id=action_item_id,
            reviewer_id=actor_id,
            action=payload.action,
            decision=payload.decision,
            review_status=payload.review_status,
            task_description=payload.task_description,
            owner_name=payload.owner_name,
            confirmed_employee_id=payload.confirmed_employee_id,
            deadline_at=payload.deadline_at,
            deadline_text=payload.deadline_text,
            priority=payload.priority,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return _single_action_item_response(
        updated_item,
        meeting_id,
        updated_item.extraction.transcript_id,
    )


@router.get(
    "/{meeting_id}/action-items",
    response_model=MeetingActionItemsResponse,
    summary="Retrieve persisted action items for a meeting",
)
def get_meeting_action_items(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can access action items",
        )

    transcript = get_meeting_transcript(db, meeting_id)
    if transcript is None or transcript.status != TRANSCRIPT_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Completed transcript not found",
        )

    extraction = (
        db.query(MeetingActionItemExtraction)
        .filter(MeetingActionItemExtraction.transcript_id == transcript.id)
        .one_or_none()
    )
    if extraction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting action items not found",
        )

    refresh_missing_employee_matches(db, extraction)
    return _meeting_action_items_response(extraction)


@router.get(
        "/{meeting_id}/transcript",
    response_model=MeetingTranscriptResponse,
)
def get_transcript(
    meeting_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    transcript_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if meeting.organizer_id != principal.user_id and not _is_assigned_task_viewer(
        db,
        meeting_id,
        principal.email,
        transcript_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Organizer or assigned task employee access required",
        )

    transcript = get_meeting_transcript(db, meeting_id, transcript_id)

    if transcript is None:
        raise HTTPException(
            status_code=404,
            detail="Transcript not found",
        )

    return MeetingTranscriptResponse(
        id=transcript.id,
        meeting_id=meeting_id,
        recording_id=transcript.recording_id,
        status=transcript.status,
        language=transcript.language,
        transcript_text=transcript.transcript_text,
        segments=[
            TranscriptSegment(**segment)
            for segment in (transcript.segments or [])
        ],
        error_message=transcript.error_message,
        created_at=transcript.created_at,
        processing_started_at=transcript.processing_started_at,
        completed_at=transcript.completed_at,
    )
@router.patch(
    "/{meeting_id}/transcript",
    response_model=MeetingTranscriptResponse,
)
def update_transcript(
    meeting_id: str,
    request: MeetingTranscriptUpdateRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    transcript_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if meeting.organizer_id != actor_id:
        raise HTTPException(status_code=403, detail="Organizer access required")

    try:
        transcript = update_meeting_transcript(
            db,
            meeting_id,
            segments=[
                segment.model_dump(exclude_unset=True)
                for segment in request.segments
            ],
            editor_id=actor_id,
            transcript_id=transcript_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if transcript is None:
        raise HTTPException(
            status_code=404,
            detail="Transcript not found",
        )

    return MeetingTranscriptResponse(
        id=transcript.id,
        meeting_id=meeting_id,
        recording_id=transcript.recording_id,
        status=transcript.status,
        language=transcript.language,
        transcript_text=transcript.transcript_text,
        segments=[
            TranscriptSegment(**segment)
            for segment in (transcript.segments or [])
        ],
        error_message=transcript.error_message,
        created_at=transcript.created_at,
        processing_started_at=transcript.processing_started_at,
        completed_at=transcript.completed_at,
    )
@router.get(
    "/{meeting_id}/transcript/corrections",
    response_model=list[TranscriptCorrectionResponse],
)
def get_transcript_corrections(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    transcript_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if meeting.organizer_id != actor_id:
        raise HTTPException(status_code=403, detail="Organizer access required")

    corrections = get_meeting_transcript_corrections(
        db,
        meeting_id,
        transcript_id,
    )

    return corrections


# -- Meeting Minutes (FR-19) ---------------------------------------------------


@router.post(
    "/{meeting_id}/minutes/generate",
    response_model=MeetingMinutesResponse,
    summary="Generate or retrieve meeting minutes snapshot and populate attendee recipients",
)
@router.post(
    "/{meeting_id}/minutes",
    response_model=MeetingMinutesResponse,
    summary="Generate or retrieve meeting minutes snapshot",
)
def generate_meeting_minutes_endpoint(
    meeting_id: str,
    force_refresh: bool = Query(
        default=False,
        description="Re-aggregate content from current action items and summary (only applies when minutes are not yet approved)",
    ),
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can generate meeting minutes",
        )

    try:
        minutes = generate_or_get_meeting_minutes(db, meeting_id, force_refresh=force_refresh)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return MeetingMinutesResponse.model_validate(minutes)


@router.get(
    "/{meeting_id}/minutes",
    response_model=MeetingMinutesResponse,
    summary="Retrieve meeting minutes and per-recipient delivery status",
)
def get_meeting_minutes_endpoint(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can access meeting minutes",
        )

    minutes = get_meeting_minutes(db, meeting_id)
    if minutes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting minutes not found",
        )

    return MeetingMinutesResponse.model_validate(minutes)


@router.patch(
    "/{meeting_id}/minutes",
    response_model=MeetingMinutesResponse,
    summary="Update or edit draft or pending meeting minutes content",
)
@router.put(
    "/{meeting_id}/minutes",
    response_model=MeetingMinutesResponse,
    summary="Update or edit draft or pending meeting minutes content",
)
def update_meeting_minutes_endpoint(
    meeting_id: str,
    payload: MeetingMinutesUpdateRequest,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can edit meeting minutes",
        )

    content_action_items = None
    if payload.content_action_items is not None:
        content_action_items = [
            item.model_dump() for item in payload.content_action_items
        ]

    try:
        minutes = update_meeting_minutes_content(
            db,
            meeting_id,
            content_summary=payload.content_summary,
            content_decisions=payload.content_decisions,
            content_action_items=content_action_items,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return MeetingMinutesResponse.model_validate(minutes)


@router.post(
    "/{meeting_id}/minutes/refresh",
    response_model=MeetingMinutesResponse,
    summary="Re-aggregate meeting minutes content from current summary and confirmed action items",
)
def refresh_meeting_minutes_endpoint(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can refresh meeting minutes",
        )

    minutes = get_meeting_minutes(db, meeting_id)
    if minutes is None:
        try:
            minutes = generate_or_get_meeting_minutes(db, meeting_id, force_refresh=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    else:
        minutes = refresh_minutes_content(db, minutes)

    return MeetingMinutesResponse.model_validate(minutes)


@router.post(
    "/{meeting_id}/minutes/approve",
    response_model=MeetingMinutesResponse,
    summary="Organizer approves meeting minutes for attendee delivery",
)
def approve_meeting_minutes_endpoint(
    meeting_id: str,
    payload: MeetingMinutesApproveRequest | None = None,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can approve meeting minutes",
        )

    try:
        minutes = approve_meeting_minutes(db, meeting_id, approver_id=actor_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if "not found" in str(exc).lower() else status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return MeetingMinutesResponse.model_validate(minutes)


@router.post(
    "/{meeting_id}/minutes/send",
    response_model=MeetingMinutesDeliveryResponse,
    summary="Send approved meeting minutes to all eligible attendee recipients",
)
def send_meeting_minutes_endpoint(
    meeting_id: str,
    retry_failed: bool = Query(default=False, description="Re-attempt previously failed recipients"),
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can send meeting minutes",
        )

    try:
        recipients = send_meeting_minutes_to_recipients(
            db, meeting_id, retry_failed=retry_failed
        )
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    minutes = get_meeting_minutes(db, meeting_id)
    recipient_responses = [
        MeetingMinutesRecipientResponse.model_validate(r) for r in recipients
    ]
    sent_count = sum(1 for r in recipients if r.delivery_status == "sent")
    failed_count = sum(1 for r in recipients if r.delivery_status == "failed")
    pending_count = sum(1 for r in recipients if r.delivery_status == "pending")

    return MeetingMinutesDeliveryResponse(
        minutes_id=minutes.id,
        meeting_id=meeting_id,
        total=len(recipients),
        sent=sent_count,
        failed=failed_count,
        pending=pending_count,
        recipients=recipient_responses,
    )


@router.get(
    "/{meeting_id}/minutes/recipients",
    response_model=list[MeetingMinutesRecipientResponse],
    summary="Retrieve per-recipient delivery status for meeting minutes",
)
def get_minutes_recipients_endpoint(
    meeting_id: str,
    actor_id: str = Depends(require_authenticated_actor_id),
    db: Session = Depends(get_db),
):
    meeting = get_meeting_by_id(db, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting not found",
        )

    if meeting.organizer_id != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting organizer can view delivery status",
        )

    recipients = get_minutes_recipients(db, meeting_id)
    return [MeetingMinutesRecipientResponse.model_validate(r) for r in recipients]
