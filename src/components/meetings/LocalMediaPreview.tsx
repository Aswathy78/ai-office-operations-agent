import { useEffect, useRef, useState } from 'react'
import { Camera, CameraOff, Mic, MicOff, Video, VideoOff } from 'lucide-react'
import type { MeetingMediaState } from '../../types/meeting'

interface LocalMediaPreviewProps {
  localStream: MediaStream | null
  mediaState: MeetingMediaState
  mediaError: string | null
  startLocalMedia: () => Promise<MediaStream | null>
  stopLocalMedia: () => void
  isMicrophoneEnabled: boolean | null
  isCameraEnabled: boolean | null
  toggleMicrophone: () => boolean | null
  toggleCamera: () => boolean | null
  showPreview?: boolean
}

export function LocalMediaPreview({
  localStream,
  mediaState,
  mediaError,
  startLocalMedia,
  stopLocalMedia,
  isMicrophoneEnabled,
  isCameraEnabled,
  toggleMicrophone,
  toggleCamera,
  showPreview = true,
}: LocalMediaPreviewProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playbackError, setPlaybackError] = useState('')

  useEffect(() => {
    if (!showPreview) return
    const video = videoRef.current
    if (!video) return
    video.srcObject = localStream
    setPlaybackError('')

    if (localStream) {
      void video.play().catch(() => {
        setPlaybackError('Preview playback was blocked. Use the video player controls to start it.')
      })
    }

    return () => {
      video.srcObject = null
    }
  }, [localStream, showPreview])

  const isRequesting = mediaState === 'requesting'

  return (
    <section className="local-media-panel" aria-labelledby="local-media-title">
      <div className="local-media-heading">
        <div>
          <p className="eyebrow">This device</p>
          <h2 id="local-media-title">Camera and microphone</h2>
        </div>
        {localStream ? <span className="local-media-ready"><Mic size={14} aria-hidden="true" /> Ready</span> : null}
      </div>

      {showPreview && (
        <div className="local-video-frame">
          {localStream ? (
            <video
              ref={videoRef}
              className="local-video-preview"
              aria-label="Your local camera preview"
              autoPlay
              muted
              playsInline
            />
          ) : (
            <div className="local-video-empty" role="status">
              <CameraOff size={24} aria-hidden="true" />
              <span>{mediaState === 'stopped' ? 'Camera and microphone are stopped.' : 'Preview is off.'}</span>
            </div>
          )}
        </div>
      )}

      {mediaError && <p className="guest-error" role="alert">{mediaError}</p>}
      {showPreview && playbackError && <p className="guest-inline-note" role="status">{playbackError}</p>}

      <div className="local-media-controls" aria-label="Local media controls">
        <button
          className={`button media-toggle${isMicrophoneEnabled === false ? ' inactive' : ''}`}
          type="button"
          disabled={isMicrophoneEnabled === null || isRequesting}
          aria-label={isMicrophoneEnabled === null
            ? 'Microphone unavailable until media is enabled'
            : isMicrophoneEnabled ? 'Mute microphone' : 'Unmute microphone'}
          title={isMicrophoneEnabled === null
            ? 'Enable camera and microphone first'
            : isMicrophoneEnabled ? 'Mute microphone' : 'Unmute microphone'}
          onClick={() => toggleMicrophone()}
        >
          {isMicrophoneEnabled === false ? <MicOff size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}
          {isMicrophoneEnabled === null ? 'Mic unavailable' : isMicrophoneEnabled ? 'Mute' : 'Unmute'}
        </button>
        <button
          className={`button media-toggle${isCameraEnabled === false ? ' inactive' : ''}`}
          type="button"
          disabled={isCameraEnabled === null || isRequesting}
          aria-label={isCameraEnabled === null
            ? 'Camera unavailable until media is enabled'
            : isCameraEnabled ? 'Turn camera off' : 'Turn camera on'}
          title={isCameraEnabled === null
            ? 'Enable camera and microphone first'
            : isCameraEnabled ? 'Turn camera off' : 'Turn camera on'}
          onClick={() => toggleCamera()}
        >
          {isCameraEnabled === false ? <VideoOff size={16} aria-hidden="true" /> : <Video size={16} aria-hidden="true" />}
          {isCameraEnabled === null ? 'Camera unavailable' : isCameraEnabled ? 'Turn camera off' : 'Turn camera on'}
        </button>
      </div>

      {localStream ? (
        <button className="button secondary local-media-action" type="button" onClick={stopLocalMedia}>
          <CameraOff size={16} aria-hidden="true" /> Stop local media
        </button>
      ) : (
        <button
          className="button primary local-media-action"
          type="button"
          disabled={isRequesting}
          onClick={() => void startLocalMedia()}
        >
          {isRequesting ? 'Requesting permission…' : mediaState === 'idle' || mediaState === 'stopped'
            ? <><Camera size={16} aria-hidden="true" /> Enable Camera &amp; Microphone</>
            : <><Camera size={16} aria-hidden="true" /> Retry Camera &amp; Microphone</>}
        </button>
      )}
    </section>
  )
}