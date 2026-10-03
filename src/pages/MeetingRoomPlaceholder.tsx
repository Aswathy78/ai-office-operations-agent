import { ArrowUpRight, Video } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  getMeeting,
  joinMeetingAttendance,
  leaveMeetingAttendance,
  MeetingApiError,
  type AttendanceRecord,
} from '../api/meetings'
import { LocalMediaPreview } from '../components/meetings/LocalMediaPreview'
import { OrganizerGuestLobby } from '../components/meetings/OrganizerGuestLobby'
import { VideoGrid } from '../components/meetings/VideoGrid'
import { useAuth } from '../contexts/AuthContext'
import { useMeetingRoom } from '../hooks/useMeetingRoom'
import type { MeetingDetails } from '../types/meeting'

type MeetingLoadState =
  | { kind: 'loading' }
  | { kind: 'valid'; meeting: MeetingDetails }
  | { kind: 'invalid'; message: string }
  | { kind: 'expired'; message: string }
  | { kind: 'unavailable'; message: string }

export function MeetingRoomPlaceholder() {
  const { meetingId } = useParams()
  const { user } = useAuth()
  const [meetingState, setMeetingState] = useState<MeetingLoadState>({ kind: 'loading' })
  const [attendanceError, setAttendanceError] = useState('')
  const room = useMeetingRoom({
    meetingId: meetingId ?? '',
    enabled: meetingState.kind === 'valid' && meetingState.meeting.id === meetingId,
    displayName: user?.name,
    email: user?.email,
    userId: user?.id,
  })

  useEffect(() => {
    let active = true
    if (!meetingId) {
      setMeetingState({ kind: 'invalid', message: 'A meeting ID is required.' })
      return () => { active = false }
    }

    setMeetingState({ kind: 'loading' })
    getMeeting(meetingId)
      .then((meeting) => {
        if (active) setMeetingState({ kind: 'valid', meeting })
      })
      .catch((error: unknown) => {
        if (!active) return
        if (error instanceof MeetingApiError && error.status === 410) {
          setMeetingState({ kind: 'expired', message: error.message })
        } else if (error instanceof MeetingApiError && error.status === 404) {
          setMeetingState({ kind: 'invalid', message: error.message })
        } else {
          setMeetingState({
            kind: 'unavailable',
            message: error instanceof Error ? error.message : 'Meeting details could not be loaded.',
          })
        }
      })
    return () => { active = false }
  }, [meetingId])

  useEffect(() => {
    if (meetingState.kind !== 'valid' || meetingState.meeting.id !== meetingId || !meetingId || !user) return

    let active = true
    let attendance: AttendanceRecord | null = null
    let leaveRequested = false
    const leaveAttendance = () => {
      if (!attendance || leaveRequested) return
      leaveRequested = true
      void leaveMeetingAttendance(meetingId, attendance.id).catch(() => undefined)
    }
    window.addEventListener('pagehide', leaveAttendance)
    const joinTimer = setTimeout(() => {
      void joinMeetingAttendance(meetingId, {
        participant_name: user.name,
        participant_email: user.email,
        user_id: user.id,
        is_external_guest: false,
      }).then((record) => {
        if (active) {
          attendance = record
          setAttendanceError('')
        } else {
          void leaveMeetingAttendance(meetingId, record.id).catch(() => undefined)
        }
      }).catch((error: unknown) => {
        if (active) setAttendanceError(error instanceof Error ? error.message : 'Attendance could not be recorded.')
      })
    }, 0)

    return () => {
      active = false
      clearTimeout(joinTimer)
      window.removeEventListener('pagehide', leaveAttendance)
      leaveAttendance()
    }
  }, [meetingId, meetingState, user])

  if (meetingState.kind === 'loading') {
    return <section className="state-card" role="status"><h2>Checking meeting…</h2></section>
  }

  if (meetingState.kind === 'invalid' || meetingState.kind === 'expired' || meetingState.kind === 'unavailable') {
    const heading = meetingState.kind === 'expired'
      ? 'This meeting has expired'
      : meetingState.kind === 'invalid'
        ? 'Meeting not found'
        : 'Unable to verify this meeting'
    const explanation = meetingState.kind === 'expired'
      ? 'This meeting is no longer available.'
      : meetingState.kind === 'invalid'
        ? 'The meeting link may be invalid or the meeting may no longer be available.'
        : 'Meeting details could not be reached. Check your connection and try the link again.'
    return (
      <section className="state-card" aria-labelledby="meeting-unavailable-title">
        <div className="state-icon danger" aria-hidden="true">!</div>
        <p className="eyebrow">Meeting unavailable</p>
        <h2 id="meeting-unavailable-title">{heading}</h2>
        <p>{explanation}</p>
        {meetingState.message && <small>{meetingState.message}</small>}
        <small>Signaling and media were not started.</small>
        <Link className="button secondary" to="/home">Back to Home</Link>
      </section>
    )
  }

  return (
    <div className="meeting-room-page">
      <section className="state-card meeting-room-intro" aria-labelledby="meeting-room-title">
        <div className="state-icon" aria-hidden="true"><Video size={28} /></div>
        <p className="eyebrow">Temporary meeting-room placeholder</p>
        <h2 id="meeting-room-title">{meetingState.meeting.title}</h2>
        <p>The meeting room itself is not implemented yet. No audio or video features are active.</p>
        <small>Meeting ID: {meetingId}</small>
        <small>Signaling: {room.connectionState}; participant ID: {room.localParticipantId ?? 'pending'}; peers: {room.participants.length}</small>
        {room.lastError && <small role="status">{room.lastError}</small>}
        {attendanceError && <small role="alert">{attendanceError}</small>}
        {meetingId && <Link className="text-link" to={`/meetings/${encodeURIComponent(meetingId)}/guest`} target="_blank" rel="noreferrer">Open guest join page <ArrowUpRight size={16} aria-hidden="true" /></Link>}
      </section>
      <LocalMediaPreview
        localStream={room.localStream}
        mediaState={room.mediaState}
        mediaError={room.mediaError}
        startLocalMedia={room.startLocalMedia}
        stopLocalMedia={room.stopLocalMedia}
        isMicrophoneEnabled={room.isMicrophoneEnabled}
        isCameraEnabled={room.isCameraEnabled}
        toggleMicrophone={room.toggleMicrophone}
        toggleCamera={room.toggleCamera}
        showPreview={false}
      />
      <VideoGrid
        localStream={room.localStream}
        remoteStreams={room.remoteStreams}
        participants={room.participants}
        localParticipantId={room.localParticipantId}
        peerStates={room.peerStates}
        participantInfos={room.participantInfos}
        isCameraEnabled={room.isCameraEnabled}
        isMicrophoneEnabled={room.isMicrophoneEnabled}
        localDisplayName={user?.name ?? 'You'}
        isOrganizer={room.isOrganizer}
        organizerId={room.organizerId}
        onMuteParticipant={room.muteParticipant}
      />
      {meetingId && room.isOrganizer && room.organizerId && (
        <OrganizerGuestLobby
          meetingId={meetingId}
          organizerId={room.organizerId}
          connectionState={room.connectionState}
          lastMessage={room.lastMessage}
        />
      )}
    </div>
  )
}
