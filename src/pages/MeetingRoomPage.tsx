/**
 * MeetingRoomPage — full-screen, dedicated meeting experience.
 *
 * This renders OUTSIDE the AppShell (no sidebar/topbar).
 * It handles:
 *   - Attendance join/leave tracking
 *   - Organizer lobby panel (guest requests)
 *   - Full video grid with host badge/controls
 *   - Bottom control bar (mic/cam/share/leave)
 *   - Muted-by-organizer notification
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Camera,
  CameraOff,
  Check,
  Copy,
  Crown,
  Link as LinkIcon,
  LogOut,
  Mic,
  MicOff,
  PhoneOff,
  FileText,
  Users,
  Video,
  VolumeX,
  X,
} from 'lucide-react'
import {
  endMeetingForEveryone,
  getMeeting,
  getMeetingSummary,
  joinMeetingAttendance,
  leaveMeetingAttendance,
  recordMeetingRecordingConsent,
  registerParticipantTracks,
  startMeetingRecording,
  stopMeetingRecording,
  uploadMeetingRecording,
  uploadParticipantTrack,
  MeetingApiError,
  type AttendanceRecord,
  type RecordingConsentStatus,
} from '../api/meetings'
import { LocalMediaPreview } from '../components/meetings/LocalMediaPreview'
import { MeetingSummaryPanel } from '../components/meetings/MeetingSummaryPanel'
import { OrganizerGuestLobby } from '../components/meetings/OrganizerGuestLobby'
import { TranscriptPanel } from '../components/meetings/TranscriptPanel'
import { useAuth } from '../contexts/AuthContext'
import { useMeetingRecording } from '../hooks/useMeetingRecording'
import {
  persistRecordingConsent,
  getRecordingUploadMessage,
  isMeetingOrganizer,
  shouldShowRecordingControls,
  shouldShowStartRecording,
  startRecordingAfterConsent,
  stopRecordingAndUpload,
  type RecordingUploadState,
} from '../hooks/meetingRecordingFlow'
import { useMeetingRoom } from '../hooks/useMeetingRoom'
import type {
  MeetingDetails,
  MeetingSummary,
  ParticipantInfo,
  ParticipantPeerState,
} from '../types/meeting'

type PageState =
  | { kind: 'loading' }
  | { kind: 'prejoin'; meeting: MeetingDetails }
  | { kind: 'meeting'; meeting: MeetingDetails }
  | { kind: 'left' }
  | { kind: 'invalid'; message: string }
  | { kind: 'expired'; message: string }

// ── Utilities ──────────────────────────────────────────────────────────────────

function getInitials(name: string) {
  return name.split(' ').filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('')
}

function formatDuration(startMs: number) {
  const diff = Math.max(0, Date.now() - startMs)
  const h = Math.floor(diff / 3600000)
  const m = Math.floor((diff % 3600000) / 60000)
  const s = Math.floor((diff % 60000) / 1000)
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function VideoTile({
  label,
  stream,
  muted = false,
  isLocal = false,
  isOrganizer = false,
  isMicrophoneEnabled,
  isCameraEnabled,
  canMute = false,
  onMute,
}: {
  label: string
  stream: MediaStream | null
  muted?: boolean
  isLocal?: boolean
  isOrganizer?: boolean
  isMicrophoneEnabled?: boolean | null
  isCameraEnabled?: boolean | null
  canMute?: boolean
  onMute?: () => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playBlocked, setPlayBlocked] = useState(false)
  const hasVideo = Boolean(stream?.getVideoTracks().some((t) => t.enabled && t.readyState === 'live'))
  const camOff = isLocal ? isCameraEnabled !== true : (isCameraEnabled === false || (!hasVideo && stream !== null))
  const micOff = isMicrophoneEnabled === false

  useEffect(() => {
    const vid = videoRef.current
    if (!vid) return
    vid.srcObject = stream
    if (stream) {
      vid.play().then(() => setPlayBlocked(false)).catch(() => setPlayBlocked(true))
    }
    return () => { vid.pause(); vid.srcObject = null }
  }, [stream])

  return (
    <div className={`meet-tile${isOrganizer ? ' meet-tile--host' : ''}${isLocal ? ' meet-tile--local' : ''}`}>
      <div className="meet-tile-video-wrap">
        {stream && !camOff
          ? <video ref={videoRef} className="meet-tile-video" autoPlay playsInline muted={muted} aria-label={`${label} video`} />
          : (
            <div className="meet-tile-avatar">
              <span className="meet-tile-initials" aria-hidden="true">{getInitials(label) || '?'}</span>
              {camOff && <span className="meet-tile-cam-off"><CameraOff size={13} aria-hidden="true" /> Camera off</span>}
            </div>
          )
        }

        {playBlocked && (
          <button className="meet-tile-play-prompt" onClick={() => videoRef.current?.play().then(() => setPlayBlocked(false)).catch(() => {})}>
            Click to play video
          </button>
        )}

        {isOrganizer && (
          <div className="meet-tile-host-badge"><Crown size={10} aria-hidden="true" /> Host</div>
        )}

        {micOff && (
          <div className="meet-tile-mic-off" aria-label={`${label} is muted`}><MicOff size={12} aria-hidden="true" /></div>
        )}
      </div>

      <div className="meet-tile-footer">
        <span className="meet-tile-name">{isOrganizer ? `Host • ${label}` : label}{isLocal ? ' (You)' : ''}</span>
        <div className="meet-tile-icons">
          {micOff
            ? <MicOff size={12} className="meet-icon-muted" aria-hidden="true" />
            : isMicrophoneEnabled === true
              ? <Mic size={12} className="meet-icon-active" aria-hidden="true" />
              : null
          }
          {canMute && !isLocal && onMute && (
            <button className="meet-tile-mute-btn" title={`Mute ${label}`} onClick={onMute}>
              <VolumeX size={12} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export function MeetingRoomPage() {
  const { meetingId = '' } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [pageState, setPageState] = useState<PageState>({ kind: 'loading' })
  const [meetingSummary, setMeetingSummary] = useState<MeetingSummary | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryError, setSummaryError] = useState('')
  const [attendanceError, setAttendanceError] = useState('')
  const [joinedMeeting, setJoinedMeeting] = useState(false)
  const [showLobby, setShowLobby] = useState(false)
  const [showTranscript, setShowTranscript] = useState(false)
  const [showEndConfirm, setShowEndConfirm] = useState(false)
  const [showAskUnmutePrompt, setShowAskUnmutePrompt] = useState(false)
  const [endingMeeting, setEndingMeeting] = useState(false)
  const [showSharePanel, setShowSharePanel] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const [mutedNotification, setMutedNotification] = useState(false)
  const [attendanceRecord, setAttendanceRecord] = useState<AttendanceRecord | null>(null)
  const [recordingConsent, setRecordingConsent] = useState<'unknown' | 'consented' | 'declined'>('unknown')
  const [recordingBusy, setRecordingBusy] = useState(false)
  const [recordingUploadState, setRecordingUploadState] = useState<RecordingUploadState | 'idle'>('idle')
  const [recordingMessage, setRecordingMessage] = useState('')
  const [duration, setDuration] = useState('0:00')
  const meetingStartRef = useRef(Date.now())
  const attendanceRef = useRef<AttendanceRecord | null>(null)

  const meeting = pageState.kind === 'meeting' || pageState.kind === 'prejoin' ? pageState.meeting : null
  const inMeeting = pageState.kind === 'meeting'

  const room = useMeetingRoom({
    meetingId,
    enabled: inMeeting,
    displayName: user?.name,
    email: user?.email,
    userId: user?.id,
  })
  const recording = useMeetingRecording(room.localStream, {
    remoteStreams: room.remoteStreams,
    participantInfos: room.participantInfos,
    localParticipant: {
      connectionId: room.localParticipantId,
      participantName: user?.name || user?.email || 'Organizer',
      attendanceId: attendanceRecord?.id,
    },
  })

  // Show muted notification
  useEffect(() => {
    if (room.isMutedByOrganizer) {
      setMutedNotification(true)
      const t = setTimeout(() => setMutedNotification(false), 5000)
      return () => clearTimeout(t)
    }
  }, [room.isMutedByOrganizer])

  // Show ask-to-unmute prompt when host requests it
  useEffect(() => {
    if (room.isAskedToUnmute) {
      setShowAskUnmutePrompt(true)
    }
  }, [room.isAskedToUnmute])

  // Handle meeting_ended from server (host ended it)
  useEffect(() => {
    if (room.meetingEnded) {
      // Clean up, then navigate to meetings page after a brief moment
      room.stopLocalMedia()
      room.disconnect()
      if (attendanceRef.current && meetingId) {
        void leaveMeetingAttendance(meetingId, attendanceRef.current.id).catch(() => undefined)
        attendanceRef.current = null
      }
      const t = setTimeout(() => navigate('/meetings', { replace: true }), 2500)
      return () => clearTimeout(t)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.meetingEnded])

  // Meeting duration timer
  useEffect(() => {
    if (!inMeeting) return
    meetingStartRef.current = Date.now()
    const t = setInterval(() => setDuration(formatDuration(meetingStartRef.current)), 1000)
    return () => clearInterval(t)
  }, [inMeeting])

  // Load meeting details
  useEffect(() => {
    let active = true
    getMeeting(meetingId)
      .then((m) => {
        if (active) setPageState({ kind: 'prejoin', meeting: m })
      })
      .catch((err: unknown) => {
        if (!active) return
        if (err instanceof MeetingApiError && err.status === 410) {
          setPageState({ kind: 'expired', message: 'This meeting has expired.' })
        } else if (err instanceof MeetingApiError && err.status === 404) {
          setPageState({ kind: 'invalid', message: 'Meeting not found.' })
        } else {
          setPageState({ kind: 'invalid', message: 'Unable to load meeting.' })
        }
      })
    return () => { active = false }
  }, [meetingId])

  useEffect(() => {
    if (!meetingId || !meeting) return
    let active = true
    setSummaryLoading(true)
    setMeetingSummary(null)
    setSummaryError('')
    getMeetingSummary(meetingId, meeting.organizer_id)
      .then((summary) => {
        if (active) setMeetingSummary(summary)
      })
      .catch((error: unknown) => {
        if (!active) return
        if (error instanceof MeetingApiError && error.status === 404) {
          setMeetingSummary(null)
          return
        }
        setSummaryError(
          error instanceof Error ? error.message : 'Meeting summary could not be loaded.',
        )
      })
      .finally(() => {
        if (active) setSummaryLoading(false)
      })
    return () => { active = false }
  }, [meetingId, meeting])

  // Attendance tracking
  useEffect(() => {
    if (!inMeeting || !meetingId || !user) return
    let active = true
    let leaveRequested = false

    const leaveAttendance = () => {
      if (!attendanceRef.current || leaveRequested) return
      leaveRequested = true
      void leaveMeetingAttendance(meetingId, attendanceRef.current.id).catch(() => undefined)
    }

    window.addEventListener('pagehide', leaveAttendance)
    void joinMeetingAttendance(meetingId, {
      participant_name: user.name,
      participant_email: user.email,
      user_id: user.id,
      is_external_guest: false,
    }).then((record) => {
      if (active) {
        attendanceRef.current = record
        setAttendanceRecord(record)
        setAttendanceError('')
      }
      else void leaveMeetingAttendance(meetingId, record.id).catch(() => undefined)
    }).catch((err: unknown) => {
      if (active) setAttendanceError(err instanceof Error ? err.message : 'Attendance could not be recorded.')
    })

    return () => {
      active = false
      window.removeEventListener('pagehide', leaveAttendance)
      leaveAttendance()
    }
  }, [inMeeting, meetingId, user])

  const handleJoin = useCallback(async () => {
    setPageState((s) => s.kind === 'prejoin' ? { kind: 'meeting', meeting: s.meeting } : s)
    setJoinedMeeting(true)
  }, [])

  const handleRecordingConsent = useCallback(async (consentStatus: RecordingConsentStatus) => {
    const attendance = attendanceRef.current
    if (!meetingId || !attendance) {
      setRecordingMessage('Your participant attendance is still being registered. Try again shortly.')
      return
    }

    setRecordingBusy(true)
    setRecordingMessage('')
    try {
      const consented = await persistRecordingConsent(
        recordMeetingRecordingConsent,
        meetingId,
        attendance.id,
        consentStatus,
      )
      setRecordingConsent(consented ? 'consented' : 'declined')
      setRecordingMessage(
        consented
          ? 'Your consent to record has been saved.'
          : 'Your decision not to consent has been saved.',
      )
    } catch (error) {
      setRecordingMessage(error instanceof Error ? error.message : 'Could not save your recording decision.')
    } finally {
      setRecordingBusy(false)
    }
  }, [meetingId])

  const handleStartRecording = useCallback(async () => {
    const organizerId = room.organizerId ?? meeting?.organizer_id ?? user?.id
    const isOrganizer = Boolean(room.isOrganizer || (user?.id && meeting?.organizer_id === user.id))
    if (!isOrganizer || !organizerId || !meetingId) {
      setRecordingMessage('Only the meeting organizer can start recording.')
      return
    }
    if (recordingConsent !== 'consented') {
      setRecordingMessage('You must explicitly consent before the organizer can start recording.')
      return
    }
    if (!room.localStream) {
      setRecordingMessage('Enable your microphone before starting a recording.')
      return
    }

    setRecordingBusy(true)
    setRecordingMessage('')
    try {
      await startRecordingAfterConsent(
        recordingConsent === 'consented',
        () => startMeetingRecording(meetingId, organizerId),
        recording.startRecording,
        () => stopMeetingRecording(meetingId, organizerId),
      )
      setRecordingMessage('Recording is active.')
    } catch (error) {
      setRecordingMessage(error instanceof Error ? error.message : 'Could not start recording.')
    } finally {
      setRecordingBusy(false)
    }
  }, [meeting, meetingId, recording, recordingConsent, room.isOrganizer, room.localStream, room.organizerId, user])

  const handleStopRecording = useCallback(async () => {
    const organizerId = room.organizerId ?? meeting?.organizer_id ?? user?.id
    if (!organizerId || !meetingId) return

    setRecordingBusy(true)
    setRecordingMessage('')
    setRecordingUploadState('idle')
    let uploadAttempted = false
    try {
      const blob = await stopRecordingAndUpload(
        recording.stopRecordingAndGetBlob,
        () => stopMeetingRecording(meetingId, organizerId),
        (completedBlob) => uploadMeetingRecording(meetingId, organizerId, completedBlob),
        (uploadState) => {
          if (uploadState === 'uploading') uploadAttempted = true
          setRecordingUploadState(uploadState)
          setRecordingMessage(getRecordingUploadMessage(uploadState))
        },
        {
          getTracks: recording.stopRecordingAndGetTracks,
          registerTracks: (tracks) => registerParticipantTracks(meetingId, organizerId, tracks),
          uploadTrack: (recordingId, trackBlob) =>
            uploadParticipantTrack(meetingId, recordingId, organizerId, trackBlob),
          onTrackError: (err, track) => {
            console.warn(
              `Participant track upload failed for ${track.participantName || track.connectionId}:`,
              err,
            )
          },
        },
      )
      if (!blob) {
        setRecordingMessage('Recording stopped, but no completed audio was available to upload.')
      }
    } catch (error) {
      setRecordingMessage(error instanceof Error
        ? `${uploadAttempted ? 'Upload failed' : 'Could not stop recording'}: ${error.message}`
        : 'Could not stop or upload recording.')
    } finally {
      setRecordingBusy(false)
    }
  }, [meeting, meetingId, recording, room.organizerId, user])

  const handleLeave = useCallback(() => {
    room.stopLocalMedia()
    room.disconnect()
    if (attendanceRef.current && meetingId) {
      void leaveMeetingAttendance(meetingId, attendanceRef.current.id).catch(() => undefined)
      attendanceRef.current = null
    }
    navigate('/meetings', { replace: true })
  }, [room, meetingId, navigate])

  const handleConfirmEndMeeting = useCallback(async () => {
    if (!meetingId || !user) return
    setEndingMeeting(true)
    try {
      await endMeetingForEveryone(meetingId, user.id)
      // The WS will receive meeting_ended and clean up; navigate immediately for the host
      room.stopLocalMedia()
      room.disconnect()
      if (attendanceRef.current) {
        void leaveMeetingAttendance(meetingId, attendanceRef.current.id).catch(() => undefined)
        attendanceRef.current = null
      }
      navigate('/meetings', { replace: true })
    } catch (err) {
      setEndingMeeting(false)
      setShowEndConfirm(false)
      alert(err instanceof Error ? err.message : 'Could not end the meeting.')
    }
  }, [meetingId, user, room, navigate])

  async function copyLink() {
    const link = meeting ? meeting.id
      ? `${window.location.origin}/meetings/${encodeURIComponent(meeting.id)}`
      : window.location.href
      : window.location.href
    try {
      await navigator.clipboard.writeText(link)
      setLinkCopied(true)
      setTimeout(() => setLinkCopied(false), 2500)
    } catch { /* ignore */ }
  }

  // ── Error / invalid states ─────────────────────────────────────────────────
  if (pageState.kind === 'loading') {
    return (
      <div className="meet-room">
        <div className="meet-center-state" role="status">
          <div className="meet-spinner" aria-hidden="true" />
          <p>Loading meeting…</p>
        </div>
      </div>
    )
  }

  if (pageState.kind === 'invalid' || pageState.kind === 'expired') {
    return (
      <div className="meet-room">
        <div className="meet-center-state" role="alert">
          <div className="meet-error-icon" aria-hidden="true">!</div>
          <h2>{pageState.kind === 'expired' ? 'Meeting ended' : 'Meeting not found'}</h2>
          <p>{pageState.message}</p>
          <button className="button primary" onClick={() => navigate('/meetings')}>Back to meetings</button>
        </div>
      </div>
    )
  }

  if (pageState.kind === 'left') {
    return (
      <div className="meet-room">
        <div className="meet-center-state">
          <div className="meet-left-icon" aria-hidden="true">👋</div>
          <h2>You've left the meeting</h2>
          <p>Your attendance has been recorded.</p>
          <button className="button primary" onClick={() => navigate('/meetings')}>Back to meetings</button>
        </div>
      </div>
    )
  }

  // ── Pre-join screen ────────────────────────────────────────────────────────
  if (pageState.kind === 'prejoin') {
    const m = pageState.meeting
    const shareLink = `${window.location.origin}/meetings/${encodeURIComponent(m.id)}`
    return (
      <div className="meet-room">
        <div className="meet-prejoin">
          <div className="meet-prejoin-brand">
            <span className="brand-mark">AO</span>
            <span><strong>AI Office</strong><small>Operations Agent</small></span>
          </div>

          <div className="meet-prejoin-card">
            <div className="meet-prejoin-preview">
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
                showPreview
              />
            </div>

            <div className="meet-prejoin-info">
              <p className="eyebrow">Ready to join?</p>
              <h1 className="meet-prejoin-title">{m.title}</h1>
              {m.agenda && <p className="meet-prejoin-agenda">{m.agenda}</p>}
              <p className="meet-prejoin-user">Joining as <strong>{user?.name ?? 'You'}</strong></p>

              <div className="meet-prejoin-share">
                <span className="meet-prejoin-link">{shareLink}</span>
                <button className="button secondary" onClick={copyLink}>
                  {linkCopied ? <><Check size={14} /> Copied!</> : <><Copy size={14} /> Copy link</>}
                </button>
              </div>

              <button className="button primary meet-join-btn" onClick={handleJoin}>
                <Video size={16} /> {user?.id && m.organizer_id === user.id ? 'Join as host' : 'Join meeting'}
              </button>

              <button className="button minimal" style={{ color: 'var(--muted)', fontSize: '0.85rem', border: 'none', background: 'none' }} onClick={() => navigate('/meetings')}>
                Back to meetings
              </button>
            </div>
          </div>
          <MeetingSummaryPanel
            summary={meetingSummary}
            loading={summaryLoading}
            error={summaryError}
          />
        </div>
      </div>
    )
  }

  // ── Live meeting room ──────────────────────────────────────────────────────
  const m = pageState.meeting
  const isHost = Boolean(room.isOrganizer || isMeetingOrganizer(m.organizer_id, user?.id))
  const remoteIds = [...new Set([...room.participants, ...room.remoteStreams.keys(), ...room.peerStates.keys()])]
    .filter((id) => id !== room.localParticipantId)
  const totalCount = remoteIds.length + 1
  const shareLink = `${window.location.origin}/meetings/${encodeURIComponent(m.id)}`

  return (
    <div
      className="meet-room"
      data-in-meeting="true"
      data-recording-state={recording.state}
      data-recording-duration-ms={recording.durationMs}
    >
      {/* Top bar */}
      <header className="meet-topbar" aria-label="Meeting header">
        <div className="meet-topbar-left">
          <span className="meet-title">{m.title}</span>
          <span className="meet-duration" aria-live="off">{duration}</span>
          {room.lastError && <span className="meet-error-pill" role="alert">{room.lastError}</span>}
        </div>
        <div className="meet-topbar-right">
          <span className="meet-conn-pill" data-state={room.connectionState}>
            {room.connectionState === 'connected' ? 'Live' : room.connectionState === 'connecting' ? 'Connecting…' : room.connectionState}
          </span>
        </div>
      </header>

      {/* Muted-by-organizer notification */}
      {mutedNotification && (
        <div className="meet-muted-toast" role="alert">
          <MicOff size={14} aria-hidden="true" />
          Your microphone was muted by the host.
          <button onClick={() => setMutedNotification(false)} aria-label="Dismiss"><X size={14} /></button>
        </div>
      )}

      {/* Ask-to-unmute prompt (host asked politely — participant chooses) */}
      {showAskUnmutePrompt && (
        <div className="meet-muted-toast meet-ask-unmute" role="dialog" aria-label="Host unmute request" style={{ background: '#1e3a5f', gap: '10px', flexWrap: 'wrap' }}>
          <Mic size={14} aria-hidden="true" />
          <span>The host has asked you to unmute your microphone.</span>
          <button
            className="button secondary"
            style={{ padding: '4px 10px', fontSize: '12px' }}
            onClick={() => {
              room.clearAskedToUnmute()
              setShowAskUnmutePrompt(false)
              room.toggleMicrophone()
            }}
          >
            Unmute microphone
          </button>
          <button
            className="button minimal"
            style={{ padding: '4px 8px', fontSize: '12px', color: '#ccc' }}
            onClick={() => {
              room.clearAskedToUnmute()
              setShowAskUnmutePrompt(false)
            }}
          >
            Stay muted
          </button>
        </div>
      )}

      {/* Meeting ended overlay (host ended or all participants left) */}
      {room.meetingEnded && (
        <div className="meet-ended-overlay" role="alert">
          <div className="meet-center-state">
            <div style={{ fontSize: '2rem', marginBottom: '8px' }}>👋</div>
            <h2>Meeting ended</h2>
            <p>{room.meetingEndedReason ?? 'The meeting has been ended by the host.'}</p>
            <p style={{ fontSize: '13px', color: 'var(--muted)' }}>Redirecting to meetings…</p>
          </div>
        </div>
      )}

      {/* End meeting confirmation modal */}
      {showEndConfirm && (
        <div className="meet-modal-overlay" role="dialog" aria-modal="true" aria-labelledby="end-meeting-title">
          <div className="meet-modal">
            <h3 id="end-meeting-title">End meeting for everyone?</h3>
            <p>All participants will be disconnected and this meeting will be completed.</p>
            <div className="meet-modal-actions">
              <button
                className="button secondary"
                onClick={() => setShowEndConfirm(false)}
                disabled={endingMeeting}
              >
                Cancel
              </button>
              <button
                className="button danger"
                onClick={() => void handleConfirmEndMeeting()}
                disabled={endingMeeting}
              >
                {endingMeeting ? 'Ending…' : 'End meeting'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Attendance error */}
      {attendanceError && (
        <div className="meet-muted-toast" role="alert" style={{ background: '#7c1515' }}>
          {attendanceError}
        </div>
      )}

      <div className="meet-muted-toast" role="status" aria-live="polite">
        <span>
          {recordingUploadState !== 'idle'
            ? recordingUploadState === 'error' && recordingMessage
              ? recordingMessage
              : getRecordingUploadMessage(recordingUploadState)
            : recording.state === 'recording'
              ? 'Recording'
              : recordingMessage || (
                recordingConsent === 'consented'
                  ? 'You have consented to recording.'
                  : recordingConsent === 'declined'
                    ? 'You have not consented to recording.'
                    : 'Recording requires your explicit consent.'
              )}
          {recording.error && ` ${recording.error}`}
        </span>
        {recordingConsent !== 'consented' && (
          <>
            <button
              className="button secondary"
              onClick={() => void handleRecordingConsent('consented')}
              disabled={recordingBusy || !attendanceRecord}
            >
              I consent to recording
            </button>
            <button
              className="button minimal"
              onClick={() => void handleRecordingConsent('declined')}
              disabled={recordingBusy || !attendanceRecord}
            >
              I do not consent
            </button>
          </>
        )}
        {shouldShowRecordingControls(isHost, recordingConsent) && (
          recording.state === 'recording' ? (
            <button
              className="button secondary"
              onClick={() => void handleStopRecording()}
              disabled={recordingBusy}
            >
              {recordingBusy ? 'Stopping…' : 'Stop recording'}
            </button>
          ) : shouldShowStartRecording(isHost, recordingConsent, recordingUploadState) ? (
            <button
              className="button secondary"
              onClick={() => void handleStartRecording()}
              disabled={recordingBusy || recording.state === 'error'}
            >
              {recordingBusy ? 'Starting…' : 'Start recording'}
            </button>
          ) : null
        )}
      </div>


      {/* Video grid */}
      <main className="meet-grid-area" aria-label="Participants">
        <div className={`meet-grid meet-grid-${Math.min(totalCount, 5)}`}>
          {/* Local tile */}
          <VideoTile
            label={user?.name ?? 'You'}
            stream={room.localStream}
            muted
            isLocal
            isOrganizer={isHost}
            isMicrophoneEnabled={room.isMicrophoneEnabled}
            isCameraEnabled={room.isCameraEnabled}
          />
          {/* Remote tiles */}
          {remoteIds.map((pid) => {
            const info: ParticipantInfo | undefined = room.participantInfos.get(pid)
            return (
              <VideoTile
                key={pid}
                label={info?.displayName ?? `Participant ${pid.slice(0, 6)}`}
                stream={room.remoteStreams.get(pid) ?? null}
                muted={false}
                isOrganizer={info?.isOrganizer ?? false}
                isMicrophoneEnabled={info?.isMicrophoneEnabled ?? null}
                isCameraEnabled={info?.isCameraEnabled ?? null}
                canMute={isHost}
                onMute={() => room.muteParticipant(pid)}
              />
            )
          })}
        </div>
      </main>

      <MeetingSummaryPanel
        summary={meetingSummary}
        loading={summaryLoading}
        error={summaryError}
      />

      {/* Side panels */}
      {showLobby && (
        <aside className="meet-side-panel" aria-label="Participants & guests">
          <div className="meet-side-panel-header">
            <h2>{isHost ? 'Participants & guests' : 'Participants'}</h2>
            <button className="icon-button" onClick={() => setShowLobby(false)} aria-label="Close panel"><X size={16} /></button>
          </div>

          {/* Participant list */}
          <div className="meet-roster">
            <p className="meet-roster-heading">In meeting ({totalCount})</p>
            <div className="meet-roster-item">
              <div className="meet-roster-avatar">{getInitials(user?.name ?? 'You')}</div>
              <div>
                <strong>{user?.name ?? 'You'}</strong>
                {isHost && <span className="meet-host-label">Host</span>}
              </div>
            </div>
            {remoteIds.map((pid) => {
              const info = room.participantInfos.get(pid)
              const name = info?.displayName ?? `Participant ${pid.slice(0, 6)}`
              const isMuted = info?.isMicrophoneEnabled === false
              return (
                <div key={pid} className="meet-roster-item">
                  <div className="meet-roster-avatar">{getInitials(name)}</div>
                  <div>
                    <strong>{name}</strong>
                    {info?.isOrganizer && <span className="meet-host-label">Host</span>}
                  </div>
                  {isHost && (
                    <div style={{ display: 'flex', gap: '4px' }}>
                      {isMuted ? (
                        <button className="meet-roster-mute" title="Ask to unmute" onClick={() => room.askToUnmuteParticipant(pid)}>
                          <Mic size={14} aria-hidden="true" />
                        </button>
                      ) : (
                        <button className="meet-roster-mute" title="Mute" onClick={() => room.muteParticipant(pid)}>
                          <VolumeX size={14} aria-hidden="true" />
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {isHost && (
            <OrganizerGuestLobby
              meetingId={meetingId}
              organizerId={room.organizerId ?? m.organizer_id ?? user?.id ?? ''}
              connectionState={room.connectionState}
              lastMessage={room.lastMessage}
            />
          )}
        </aside>
      )}

      <TranscriptPanel
        meetingId={meetingId}
        organizerId={m.organizer_id}
        isOrganizer={isHost}
        isOpen={showTranscript}
        onClose={() => setShowTranscript(false)}
      />

      {showSharePanel && (
        <aside className="meet-side-panel" aria-label="Share meeting">
          <div className="meet-side-panel-header">
            <h2>Share meeting</h2>
            <button className="icon-button" onClick={() => setShowSharePanel(false)} aria-label="Close panel"><X size={16} /></button>
          </div>
          <div style={{ padding: '1rem' }}>
            <p style={{ margin: '0 0 12px', fontSize: '13px', color: 'var(--muted)' }}>
              Share this link so others can join:
            </p>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'stretch' }}>
              <input
                type="text"
                value={shareLink}
                readOnly
                style={{ flex: 1, padding: '8px 10px', borderRadius: '7px', border: '1px solid var(--border)', fontSize: '12px', background: '#f5f7fb' }}
                aria-label="Meeting link"
              />
              <button className="button secondary" style={{ whiteSpace: 'nowrap' }} onClick={copyLink}>
                {linkCopied ? <><Check size={14} /> Copied!</> : <><Copy size={14} /> Copy</>}
              </button>
            </div>
            {linkCopied && <p style={{ margin: '8px 0 0', color: 'var(--success)', fontSize: '12px' }}>Meeting link copied.</p>}
          </div>
        </aside>
      )}

      {/* Control bar */}
      <footer className="meet-controls" aria-label="Meeting controls">
        <div className="meet-controls-inner">
          {/* Mic */}
          <button
            className={`meet-ctrl-btn${room.isMicrophoneEnabled === false ? ' meet-ctrl-btn--off' : ''}`}
            onClick={() => { if (room.mediaState === 'idle' || room.mediaState === 'stopped') void room.startLocalMedia(); else room.toggleMicrophone() }}
            title={room.isMicrophoneEnabled === false ? 'Unmute microphone' : 'Mute microphone'}
            aria-label={room.isMicrophoneEnabled === false ? 'Unmute microphone' : 'Mute microphone'}
          >
            {room.isMicrophoneEnabled === false ? <MicOff size={20} /> : <Mic size={20} />}
            <span>{room.isMicrophoneEnabled === false ? 'Unmute' : 'Mute'}</span>
          </button>

          {/* Camera */}
          <button
            className={`meet-ctrl-btn${room.isCameraEnabled === false ? ' meet-ctrl-btn--off' : ''}`}
            onClick={() => { if (room.mediaState === 'idle' || room.mediaState === 'stopped') void room.startLocalMedia(); else room.toggleCamera() }}
            title={room.isCameraEnabled === false ? 'Turn on camera' : 'Turn off camera'}
            aria-label={room.isCameraEnabled === false ? 'Turn on camera' : 'Turn off camera'}
          >
            {room.isCameraEnabled === false ? <CameraOff size={20} /> : <Camera size={20} />}
            <span>{room.isCameraEnabled === false ? 'Camera off' : 'Camera on'}</span>
          </button>

          {/* Participants (host only gets lobby) */}
          <button
            className={`meet-ctrl-btn${showLobby ? ' meet-ctrl-btn--active' : ''}`}
            onClick={() => { setShowLobby((v) => !v); setShowSharePanel(false); setShowTranscript(false) }}
            aria-label="Show participants"
            title="Participants"
          >
            <Users size={20} />
            <span>People</span>
          </button>

          {/* Share */}
          <button
            className={`meet-ctrl-btn${showSharePanel ? ' meet-ctrl-btn--active' : ''}`}
            onClick={() => { setShowSharePanel((v) => !v); setShowLobby(false); setShowTranscript(false) }}
            aria-label="Share meeting link"
            title="Share"
          >
            <LinkIcon size={20} />
            <span>Share</span>
          </button>

          <button
            className={`meet-ctrl-btn${showTranscript ? ' meet-ctrl-btn--active' : ''}`}
            onClick={() => {
              setShowTranscript((current) => !current)
              setShowLobby(false)
              setShowSharePanel(false)
            }}
            aria-label="Show transcript"
            title="Transcript"
          >
            <FileText size={20} />
            <span>Transcript</span>
          </button>

          {/* End meeting for everyone — host only */}
          {isHost && (
            <button
              className="meet-ctrl-btn meet-ctrl-btn--end"
              onClick={() => setShowEndConfirm(true)}
              aria-label="End meeting for everyone"
              title="End meeting for everyone"
              disabled={endingMeeting}
            >
              <PhoneOff size={20} />
              <span>End</span>
            </button>
          )}

          {/* Leave */}
          <button
            className="meet-ctrl-btn meet-ctrl-btn--leave"
            onClick={handleLeave}
            aria-label="Leave meeting"
            title="Leave meeting"
          >
            <LogOut size={20} />
            <span>Leave</span>
          </button>
        </div>
      </footer>
    </div>
  )
}
