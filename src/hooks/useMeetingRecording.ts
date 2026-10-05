import { useCallback, useEffect, useRef, useState } from 'react'
import {
  MeetingRecordingSession,
  type MeetingRecordingSnapshot,
  type MultiTrackSourceConfig,
  type RecordedParticipantTrack,
} from './meetingRecordingSession'

export type { MultiTrackSourceConfig, RecordedParticipantTrack }

const INITIAL_SNAPSHOT: MeetingRecordingSnapshot = {
  state: 'idle',
  durationMs: 0,
  blob: null,
  chunks: [],
  error: null,
  tracks: [],
}

export function useMeetingRecording(
  localStream: MediaStream | null,
  options?: MultiTrackSourceConfig,
) {
  const [snapshot, setSnapshot] = useState<MeetingRecordingSnapshot>(INITIAL_SNAPSHOT)
  const sessionRef = useRef<MeetingRecordingSession | null>(null)
  const optionsRef = useRef(options)
  optionsRef.current = options

  useEffect(() => {
    const session = new MeetingRecordingSession(localStream, setSnapshot, undefined, optionsRef.current)
    sessionRef.current = session
    setSnapshot(session.snapshot)

    return () => {
      session.dispose()
      if (sessionRef.current === session) sessionRef.current = null
    }
  }, [localStream])

  useEffect(() => {
    sessionRef.current?.updateMultiTrackConfig(options)
  }, [options])

  const startRecording = useCallback(() => {
    if (optionsRef.current) {
      sessionRef.current?.updateMultiTrackConfig(optionsRef.current)
    }
    return sessionRef.current?.start() ?? false
  }, [])

  const stopRecording = useCallback(() => sessionRef.current?.stop() ?? false, [])

  const stopRecordingAndGetBlob = useCallback(
    () => sessionRef.current?.stopAndWait() ?? Promise.resolve(null),
    [],
  )

  const stopRecordingAndGetTracks = useCallback(
    () => sessionRef.current?.stopAndWaitTracks() ?? Promise.resolve([]),
    [],
  )

  const stopRecordingAndGetAll = useCallback(
    () => sessionRef.current?.stopAndWaitAll() ?? Promise.resolve({ blob: null, tracks: [] }),
    [],
  )

  return {
    ...snapshot,
    tracks: snapshot.tracks ?? [],
    startRecording,
    stopRecording,
    stopRecordingAndGetBlob,
    stopRecordingAndGetTracks,
    stopRecordingAndGetAll,
  }
}