import { useEffect, useRef, useState } from 'react'
import { CameraOff, Crown, Mic, MicOff, Play, Video, VolumeX } from 'lucide-react'
import type { ParticipantInfo, ParticipantPeerState } from '../../types/meeting'

function initials(name: string): string {
  return name
    .split(' ')
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join('')
}

interface VideoGridProps {
  localStream: MediaStream | null
  remoteStreams: ReadonlyMap<string, MediaStream>
  participants: readonly string[]
  localParticipantId: string | null
  peerStates: ReadonlyMap<string, ParticipantPeerState>
  participantInfos: ReadonlyMap<string, ParticipantInfo>
  isCameraEnabled: boolean | null
  isMicrophoneEnabled: boolean | null
  localDisplayName: string
  isOrganizer: boolean
  organizerId: string | null
  onMuteParticipant?: (participantId: string) => void
}

export function VideoGrid({
  localStream,
  remoteStreams,
  participants,
  localParticipantId,
  peerStates,
  participantInfos,
  isCameraEnabled,
  isMicrophoneEnabled,
  localDisplayName,
  isOrganizer,
  organizerId: _organizerId,
  onMuteParticipant,
}: VideoGridProps) {
  const remoteParticipantIds = [...new Set([
    ...participants,
    ...remoteStreams.keys(),
    ...peerStates.keys(),
  ])].filter((participantId) => participantId !== localParticipantId)

  const totalCount = remoteParticipantIds.length + 1

  return (
    <section className="video-grid-section" aria-labelledby="video-grid-title">
      <div className="video-grid-heading">
        <div>
          <p className="eyebrow">Meeting room</p>
          <h2 id="video-grid-title">Participants</h2>
        </div>
        <span className="video-grid-count">
          {totalCount} {totalCount === 1 ? 'participant' : 'participants'}
        </span>
      </div>

      <div className={`video-grid video-grid-count-${Math.min(totalCount, 5)}`}>
        <ParticipantVideoTile
          participantId={localParticipantId ?? 'local'}
          label={localDisplayName || 'You'}
          stream={localStream}
          muted
          connectionLabel={localStream ? 'Local' : 'Camera off'}
          isLocal
          isCameraEnabled={isCameraEnabled}
          isMicrophoneEnabled={isMicrophoneEnabled}
          isOrganizer={isOrganizer}
        />
        {remoteParticipantIds.map((participantId) => {
          const peerState = peerStates.get(participantId)
          const info = participantInfos.get(participantId)
          return (
            <ParticipantVideoTile
              key={participantId}
              participantId={participantId}
              label={info?.displayName ?? `Participant ${participantId.slice(0, 8)}`}
              stream={remoteStreams.get(participantId) ?? null}
              muted={false}
              connectionLabel={peerState?.connectionState ?? 'connecting'}
              isCameraEnabled={info?.isCameraEnabled ?? null}
              isMicrophoneEnabled={info?.isMicrophoneEnabled ?? null}
              isOrganizer={info?.isOrganizer ?? false}
              canMute={isOrganizer}
              onMute={() => onMuteParticipant?.(participantId)}
            />
          )
        })}
      </div>
    </section>
  )
}

interface ParticipantVideoTileProps {
  participantId: string
  label: string
  stream: MediaStream | null
  muted: boolean
  connectionLabel: string
  isLocal?: boolean
  isCameraEnabled?: boolean | null
  isMicrophoneEnabled?: boolean | null
  isOrganizer?: boolean
  canMute?: boolean
  onMute?: () => void
}

function ParticipantVideoTile({
  participantId,
  label,
  stream,
  muted,
  connectionLabel,
  isLocal = false,
  isCameraEnabled = null,
  isMicrophoneEnabled = null,
  isOrganizer = false,
  canMute = false,
  onMute,
}: ParticipantVideoTileProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playbackBlocked, setPlaybackBlocked] = useState(false)
  const hasVideoTrack = Boolean(stream?.getVideoTracks().length)
  const cameraIsOff = isLocal
    ? isCameraEnabled !== true
    : isCameraEnabled === false || (!hasVideoTrack && stream != null)
  const micIsOff = isMicrophoneEnabled === false

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    let active = true
    video.srcObject = stream
    setPlaybackBlocked(false)

    if (stream) {
      void video.play().then(() => {
        if (active) setPlaybackBlocked(false)
      }).catch(() => {
        if (active) setPlaybackBlocked(true)
      })
    }

    return () => {
      active = false
      video.pause()
      video.srcObject = null
    }
  }, [stream])

  async function retryPlayback() {
    const video = videoRef.current
    if (!video) return
    try {
      await video.play()
      setPlaybackBlocked(false)
    } catch {
      setPlaybackBlocked(true)
    }
  }

  const avatarText = initials(label) || label.slice(0, 2).toUpperCase()

  return (
    <article
      className={`participant-tile${isLocal ? ' local-participant-tile' : ''}${isOrganizer ? ' organizer-tile' : ''}`}
      data-participant-id={participantId}
    >
      <div className="participant-video-frame">
        {stream && !cameraIsOff ? (
          <video
            ref={videoRef}
            className={`participant-video${isLocal ? ' self-video' : ''}`}
            aria-label={`${label} video`}
            autoPlay
            muted={muted}
            playsInline
          />
        ) : (
          <div className="participant-video-empty" role="status">
            <div className="participant-avatar" aria-hidden="true">{avatarText}</div>
            {cameraIsOff && (
              <span className="participant-camera-off-label">
                <CameraOff size={14} aria-hidden="true" /> Camera off
              </span>
            )}
            {!stream && !isLocal && (
              <span>{connectionLabel}</span>
            )}
          </div>
        )}

        {/* Microphone muted indicator */}
        {micIsOff && (
          <div className="participant-mic-off" aria-label={`${label} is muted`}>
            <MicOff size={14} aria-hidden="true" />
          </div>
        )}

        {/* Playback blocked prompt */}
        {stream && playbackBlocked && (
          <div className="participant-playback-prompt">
            <span>Playback needs your permission.</span>
            <button className="participant-play-button" type="button" onClick={() => void retryPlayback()}>
              <Play size={15} aria-hidden="true" /> Play media
            </button>
          </div>
        )}

        {/* Organizer badge */}
        {isOrganizer && (
          <div className="participant-organizer-badge" aria-label="Organizer">
            <Crown size={11} aria-hidden="true" /> Organizer
          </div>
        )}
      </div>

      <div className="participant-tile-footer">
        <div className="participant-tile-name">
          <strong title={label}>{label}</strong>
          {isLocal && <span className="participant-you-label">You</span>}
        </div>
        <div className="participant-tile-status">
          {micIsOff
            ? <MicOff size={13} aria-hidden="true" className="status-icon muted" />
            : isMicrophoneEnabled === true
              ? <Mic size={13} aria-hidden="true" className="status-icon active" />
              : null
          }
          {canMute && !isLocal && onMute && (
            <button
              className="participant-mute-btn"
              type="button"
              title={`Mute ${label}`}
              aria-label={`Mute ${label}`}
              onClick={onMute}
            >
              <VolumeX size={13} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </article>
  )
}