/**
 * GuestMeetingRoomPlaceholder — Dedicated full-screen meeting experience for admitted external guests.
 *
 * Renders the same Google-Meet-inspired meeting UI as authenticated users:
 * - Top bar with meeting title, duration, and connection status
 * - Large responsive participant video grid with host badge and mic/camera indicators
 * - Bottom floating control bar (Mic, Camera, People, Share, Leave)
 * - Muted-by-host notification toast
 * - Complete WebRTC media and attendance cleanup on Leave
 */
import { useCallback, useEffect, useRef, useState } from 'react'
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
  Users,
  Video,
  X,
} from 'lucide-react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  getMeeting,
  joinMeetingAttendance,
  leaveMeetingAttendance,
  MeetingApiError,
  type AttendanceRecord,
} from '../api/meetings'
import { useMeetingRoom } from '../hooks/useMeetingRoom'
import type { GuestRequest, MeetingDetails, ParticipantInfo } from '../types/meeting'

type GuestRoomState =
  | { kind: 'loading' }
  | { kind: 'valid'; meeting: MeetingDetails; request: GuestRequest }
  | { kind: 'unauthorized'; message: string }
  | { kind: 'invalid'; message: string }
  | { kind: 'expired'; message: string }
  | { kind: 'unavailable'; message: string }

function readAdmittedRequest(meetingId: string, requestId: string | null): GuestRequest | null {
  if (!requestId) return null
  try {
    const stored: unknown = JSON.parse(sessionStorage.getItem(`meeting-guest-request:${meetingId}`) ?? 'null')
    if (
      typeof stored === 'object' && stored !== null &&
      'id' in stored && stored.id === requestId &&
      'meeting_id' in stored && stored.meeting_id === meetingId &&
      'guest_name' in stored && typeof stored.guest_name === 'string' &&
      'status' in stored && stored.status === 'admitted'
    ) {
      return stored as GuestRequest
    }
  } catch {
    return null
  }
  return null
}

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

function GuestVideoTile({
  label,
  stream,
  muted = false,
  isLocal = false,
  isOrganizer = false,
  isMicrophoneEnabled,
  isCameraEnabled,
}: {
  label: string
  stream: MediaStream | null
  muted?: boolean
  isLocal?: boolean
  isOrganizer?: boolean
  isMicrophoneEnabled?: boolean | null
  isCameraEnabled?: boolean | null
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
        <span className="meet-tile-name">{label}{isLocal ? ' (You)' : ''}</span>
        <div className="meet-tile-icons">
          {micOff
            ? <MicOff size={12} className="meet-icon-muted" aria-hidden="true" />
            : isMicrophoneEnabled === true
              ? <Mic size={12} className="meet-icon-active" aria-hidden="true" />
              : null
          }
        </div>
      </div>
    </div>
  )
}

export function GuestMeetingRoomPlaceholder() {
  const { meetingId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const requestId = searchParams.get('requestId')
  const navigate = useNavigate()
  const [roomState, setRoomState] = useState<GuestRoomState>({ kind: 'loading' })
  const [isLeft, setIsLeft] = useState(false)
  const [attendanceError, setAttendanceError] = useState('')
  const [showRoster, setShowRoster] = useState(false)
  const [showSharePanel, setShowSharePanel] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const [mutedNotification, setMutedNotification] = useState(false)
  const [duration, setDuration] = useState('0:00')
  const meetingStartRef = useRef(Date.now())
  const attendanceRef = useRef<AttendanceRecord | null>(null)

  const room = useMeetingRoom({
    meetingId,
    enabled: roomState.kind === 'valid' && roomState.meeting.id === meetingId && !isLeft,
    displayName: roomState.kind === 'valid' ? roomState.request.guest_name : undefined,
    email: roomState.kind === 'valid' ? roomState.request.guest_email : undefined,
    userId: null,
  })

  // Show muted notification
  useEffect(() => {
    if (room.isMutedByOrganizer) {
      setMutedNotification(true)
      const t = setTimeout(() => setMutedNotification(false), 5000)
      return () => clearTimeout(t)
    }
  }, [room.isMutedByOrganizer])

  // Meeting duration timer
  useEffect(() => {
    if (roomState.kind !== 'valid' || isLeft) return
    meetingStartRef.current = Date.now()
    const t = setInterval(() => setDuration(formatDuration(meetingStartRef.current)), 1000)
    return () => clearInterval(t)
  }, [roomState.kind, isLeft])

  useEffect(() => {
    let active = true
    const request = readAdmittedRequest(meetingId, requestId)
    if (!request) {
      setRoomState({ kind: 'unauthorized', message: 'An admitted guest request is required to enter this meeting.' })
      return () => { active = false }
    }

    setRoomState({ kind: 'loading' })
    getMeeting(meetingId)
      .then((meeting) => {
        if (active) setRoomState({ kind: 'valid', meeting, request })
      })
      .catch((error: unknown) => {
        if (!active) return
        if (error instanceof MeetingApiError && error.status === 410) {
          setRoomState({ kind: 'expired', message: error.message })
        } else if (error instanceof MeetingApiError && error.status === 404) {
          setRoomState({ kind: 'invalid', message: error.message })
        } else {
          setRoomState({
            kind: 'unavailable',
            message: error instanceof Error ? error.message : 'Meeting details could not be loaded.',
          })
        }
      })
    return () => { active = false }
  }, [meetingId, requestId])

  useEffect(() => {
    if (roomState.kind !== 'valid' || roomState.meeting.id !== meetingId || isLeft) return
    let active = true
    let leaveRequested = false
    const { request } = roomState
    const leaveAttendance = () => {
      if (!attendanceRef.current || leaveRequested) return
      leaveRequested = true
      void leaveMeetingAttendance(meetingId, attendanceRef.current.id).catch(() => undefined)
    }
    window.addEventListener('pagehide', leaveAttendance)
    const joinTimer = setTimeout(() => {
      void joinMeetingAttendance(meetingId, {
        participant_name: request.guest_name,
        participant_email: request.guest_email,
        is_external_guest: true,
        guest_request_id: request.id,
      }).then((record) => {
        if (active) {
          attendanceRef.current = record
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
  }, [meetingId, roomState, isLeft])

  const handleLeave = useCallback(() => {
    room.stopLocalMedia()
    room.disconnect()
    if (attendanceRef.current && meetingId) {
      void leaveMeetingAttendance(meetingId, attendanceRef.current.id).catch(() => undefined)
      attendanceRef.current = null
    }
    setIsLeft(true)
  }, [room, meetingId])

  async function copyLink() {
    const link = `${window.location.origin}/meetings/${encodeURIComponent(meetingId)}`
    try {
      await navigator.clipboard.writeText(link)
      setLinkCopied(true)
      setTimeout(() => setLinkCopied(false), 2500)
    } catch { /* ignore */ }
  }

  if (isLeft) {
    return (
      <div className="meet-room">
        <div className="meet-center-state">
          <div className="meet-left-icon" aria-hidden="true">👋</div>
          <h2>You've left the meeting</h2>
          <p>Your attendance has been recorded.</p>
          <button className="button primary" onClick={() => navigate('/')}>Back to Home</button>
        </div>
      </div>
    )
  }

  if (roomState.kind === 'loading') {
    return (
      <div className="meet-room">
        <div className="meet-center-state" role="status">
          <div className="meet-spinner" aria-hidden="true" />
          <p>Joining meeting…</p>
        </div>
      </div>
    )
  }

  if (
    roomState.kind === 'unauthorized' ||
    roomState.kind === 'invalid' ||
    roomState.kind === 'expired' ||
    roomState.kind === 'unavailable'
  ) {
    const heading = roomState.kind === 'unauthorized'
      ? 'Organizer admission required'
      : roomState.kind === 'expired'
        ? 'This meeting has expired'
        : roomState.kind === 'invalid'
          ? 'Meeting not found'
          : 'Unable to verify this meeting'
    const explanation = roomState.kind === 'unauthorized'
      ? roomState.message
      : roomState.kind === 'expired'
        ? 'This meeting is no longer available.'
        : roomState.kind === 'invalid'
          ? 'The meeting link may be invalid or the meeting may no longer be available.'
          : 'Meeting details could not be reached. Check your connection and try again.'
    return (
      <div className="meet-room">
        <div className="meet-center-state" role="alert">
          <div className="meet-error-icon" aria-hidden="true">!</div>
          <h2>{heading}</h2>
          <p>{explanation}</p>
          {roomState.kind === 'unauthorized' ? (
            <Link className="button secondary" to={`/meetings/${encodeURIComponent(meetingId)}`}>Back to lobby</Link>
          ) : (
            <Link className="button secondary" to="/">Back to Home</Link>
          )}
        </div>
      </div>
    )
  }

  const { meeting, request } = roomState
  const remoteIds = [...new Set([...room.participants, ...room.remoteStreams.keys(), ...room.peerStates.keys()])]
    .filter((id) => id !== room.localParticipantId)
  const totalCount = remoteIds.length + 1
  const shareLink = `${window.location.origin}/meetings/${encodeURIComponent(meeting.id)}`

  return (
    <div className="meet-room" data-in-meeting="true">
      {/* Top bar */}
      <header className="meet-topbar" aria-label="Meeting header">
        <div className="meet-topbar-left">
          <span className="meet-title">{meeting.title}</span>
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

      {/* Attendance error */}
      {attendanceError && (
        <div className="meet-muted-toast" role="alert" style={{ background: '#7c1515' }}>
          {attendanceError}
        </div>
      )}

      {/* Video grid */}
      <main className="meet-grid-area" aria-label="Participants">
        <div className={`meet-grid meet-grid-${Math.min(totalCount, 5)}`}>
          {/* Local tile (Guest) */}
          <GuestVideoTile
            label={request.guest_name}
            stream={room.localStream}
            muted
            isLocal
            isOrganizer={false}
            isMicrophoneEnabled={room.isMicrophoneEnabled}
            isCameraEnabled={room.isCameraEnabled}
          />
          {/* Remote tiles */}
          {remoteIds.map((pid) => {
            const info: ParticipantInfo | undefined = room.participantInfos.get(pid)
            return (
              <GuestVideoTile
                key={pid}
                label={info?.displayName ?? `Participant ${pid.slice(0, 6)}`}
                stream={room.remoteStreams.get(pid) ?? null}
                muted={false}
                isOrganizer={info?.isOrganizer ?? false}
                isMicrophoneEnabled={info?.isMicrophoneEnabled ?? null}
                isCameraEnabled={info?.isCameraEnabled ?? null}
              />
            )
          })}
        </div>
      </main>

      {/* Side panels */}
      {showRoster && (
        <aside className="meet-side-panel" aria-label="Participants list">
          <div className="meet-side-panel-header">
            <h2>Participants ({totalCount})</h2>
            <button className="icon-button" onClick={() => setShowRoster(false)} aria-label="Close panel"><X size={16} /></button>
          </div>
          <div className="meet-roster">
            <div className="meet-roster-item">
              <div className="meet-roster-avatar">{getInitials(request.guest_name)}</div>
              <div><strong>{request.guest_name}</strong></div>
            </div>
            {remoteIds.map((pid) => {
              const info = room.participantInfos.get(pid)
              const name = info?.displayName ?? `Participant ${pid.slice(0, 6)}`
              return (
                <div key={pid} className="meet-roster-item">
                  <div className="meet-roster-avatar">{getInitials(name)}</div>
                  <div>
                    <strong>{name}</strong>
                    {info?.isOrganizer && <span className="meet-host-label">Host</span>}
                  </div>
                </div>
              )
            })}
          </div>
        </aside>
      )}

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

          {/* Participants */}
          <button
            className={`meet-ctrl-btn${showRoster ? ' meet-ctrl-btn--active' : ''}`}
            onClick={() => { setShowRoster((v) => !v); setShowSharePanel(false) }}
            aria-label="Show participants"
            title="Participants"
          >
            <Users size={20} />
            <span>People</span>
          </button>

          {/* Share */}
          <button
            className={`meet-ctrl-btn${showSharePanel ? ' meet-ctrl-btn--active' : ''}`}
            onClick={() => { setShowSharePanel((v) => !v); setShowRoster(false) }}
            aria-label="Share meeting link"
            title="Share"
          >
            <LinkIcon size={20} />
            <span>Share</span>
          </button>

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