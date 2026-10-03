import { useCallback, useEffect, useRef, useState } from 'react'
import {
  MeetingRecordingSession,
  type MeetingRecordingSnapshot,
} from './meetingRecordingSession'

const INITIAL_SNAPSHOT: MeetingRecordingSnapshot = {
  state: 'idle',
  durationMs: 0,
  blob: null,
  chunks: [],
  error: null,
}

export function useMeetingRecording(localStream: MediaStream | null) {
  const [snapshot, setSnapshot] = useState(INITIAL_SNAPSHOT)
  const sessionRef = useRef<MeetingRecordingSession | null>(null)

  useEffect(() => {
    const session = new MeetingRecordingSession(localStream, setSnapshot)
    sessionRef.current = session
    setSnapshot(session.snapshot)

    return () => {
      session.dispose()
      if (sessionRef.current === session) sessionRef.current = null
    }
  }, [localStream])

  const startRecording = useCallback(() => sessionRef.current?.start() ?? false, [])
  const stopRecording = useCallback(() => sessionRef.current?.stop() ?? false, [])
  const stopRecordingAndGetBlob = useCallback(
    () => sessionRef.current?.stopAndWait() ?? Promise.resolve(null),
    [],
  )

  return {
    ...snapshot,
    startRecording,
    stopRecording,
    stopRecordingAndGetBlob,
  }
}