import type {
  MeetingRecordingRecord,
  MeetingRecordingUploadResponse,
  ParticipantTrackRegistration,
  ParticipantTrackRegistrationResponse,
  RecordingConsentRecord,
  RecordingConsentStatus,
} from '../api/meetings'
import type { RecordedParticipantTrack } from './meetingRecordingSession'

export type RecordingUploadState = 'uploading' | 'completed' | 'processing' | 'failed' | 'error'

export function getRecordingUploadState(
  status: MeetingRecordingUploadResponse['status'],
): Exclude<RecordingUploadState, 'uploading' | 'error'> {
  switch (status) {
    case 'completed':
    case 'processing':
    case 'failed':
      return status
    default:
      throw new Error(`Unexpected recording upload status: ${status}`)
  }
}

export function getRecordingUploadMessage(state: RecordingUploadState): string {
  switch (state) {
    case 'uploading':
      return 'Uploading recording…'
    case 'completed':
      return 'Uploaded / Completed'
    case 'processing':
      return 'Uploaded / Processing'
    case 'failed':
      return 'Upload failed. The recording was marked as failed.'
    case 'error':
      return 'Upload failed. The completed recording remains in this browser session.'
  }
}

export function isMeetingOrganizer(
  organizerId: string | null | undefined,
  userId: string | null | undefined,
): boolean {
  return Boolean(organizerId && userId && organizerId === userId)
}

export function shouldShowRecordingControls(
  isOrganizer: boolean,
  consentStatus: 'unknown' | 'consented' | 'declined',
): boolean {
  return isOrganizer && consentStatus === 'consented'
}

export function shouldShowStartRecording(
  isOrganizer: boolean,
  consentStatus: 'unknown' | 'consented' | 'declined',
  uploadState: RecordingUploadState | 'idle',
): boolean {
  return shouldShowRecordingControls(isOrganizer, consentStatus) && uploadState !== 'completed'
}

export async function persistRecordingConsent(
  saveConsent: (
    meetingId: string,
    attendanceId: string,
    consentStatus: RecordingConsentStatus,
  ) => Promise<RecordingConsentRecord>,
  meetingId: string,
  attendanceId: string,
  consentStatus: RecordingConsentStatus,
): Promise<boolean> {
  const result = await saveConsent(meetingId, attendanceId, consentStatus)
  return result.consent_status === 'consented'
}

export async function startRecordingAfterConsent(
  consentConfirmed: boolean,
  startBackend: () => Promise<MeetingRecordingRecord>,
  startBrowserRecorder: () => boolean,
  stopBackend: () => Promise<MeetingRecordingRecord>,
): Promise<void> {
  if (!consentConfirmed) {
    throw new Error('Explicit recording consent is required before recording can start.')
  }

  const recording = await startBackend()
  if (recording.status !== 'recording') {
    throw new Error('The backend did not confirm that recording started.')
  }

  if (!startBrowserRecorder()) {
    try {
      await stopBackend()
    } catch {
      // Preserve the local recorder failure; the caller reports the backend issue separately.
    }
    throw new Error('The browser could not start recording. The recording request was stopped.')
  }
}

export interface ProcessParticipantTracksOptions {
  tracks: readonly RecordedParticipantTrack[]
  registerTracks: (
    registrations: ParticipantTrackRegistration[],
  ) => Promise<ParticipantTrackRegistrationResponse[]>
  uploadTrack: (
    recordingId: string,
    blob: Blob,
  ) => Promise<MeetingRecordingUploadResponse>
  onTrackError?: (error: Error, track: RecordedParticipantTrack) => void
}

export interface ParticipantTrackUploadResult {
  successful: number
  failed: number
  errors: { connectionId: string; error: Error }[]
}

export async function uploadParticipantTracks(
  options: ProcessParticipantTracksOptions,
): Promise<ParticipantTrackUploadResult> {
  const { tracks, registerTracks, uploadTrack, onTrackError } = options

  const result: ParticipantTrackUploadResult = {
    successful: 0,
    failed: 0,
    errors: [],
  }

  if (!tracks || tracks.length === 0) {
    return result
  }

  const registrationItems: ParticipantTrackRegistration[] = tracks.map((t) => ({
    connection_id: t.connectionId || t.connection_id || '',
    participant_name: t.participantName || t.participant_name || 'Participant',
    attendance_id: t.attendanceId || t.attendance_id || null,
  }))

  let registeredResponses: ParticipantTrackRegistrationResponse[] = []
  try {
    registeredResponses = await registerTracks(registrationItems)
  } catch (error) {
    const err = error instanceof Error ? error : new Error(String(error))
    tracks.forEach((t) => {
      const connId = t.connectionId || t.connection_id || ''
      result.failed += 1
      result.errors.push({ connectionId: connId, error: err })
      onTrackError?.(err, t)
    })
    return result
  }

  for (const resp of registeredResponses) {
    const matchedTrack = tracks.find(
      (t) => (t.connectionId || t.connection_id) === resp.connection_id,
    )

    if (!matchedTrack || !matchedTrack.blob) {
      continue
    }

    try {
      await uploadTrack(resp.recording_id, matchedTrack.blob)
      result.successful += 1
    } catch (error) {
      const err = error instanceof Error ? error : new Error(String(error))
      result.failed += 1
      result.errors.push({
        connectionId: resp.connection_id,
        error: err,
      })
      onTrackError?.(err, matchedTrack)
    }
  }

  return result
}

export async function stopRecordingAndUpload(
  stopBrowserRecorder:
    | (() => Promise<Blob | null>)
    | (() => Promise<{ blob: Blob | null; tracks: readonly RecordedParticipantTrack[] }>),
  stopBackend: () => Promise<MeetingRecordingRecord>,
  uploadBlob: (blob: Blob) => Promise<MeetingRecordingUploadResponse>,
  onUploadState: (state: RecordingUploadState) => void,
  participantTrackOptions?: {
    getTracks?: () => Promise<readonly RecordedParticipantTrack[]> | readonly RecordedParticipantTrack[]
    registerTracks?: (
      registrations: ParticipantTrackRegistration[],
    ) => Promise<ParticipantTrackRegistrationResponse[]>
    uploadTrack?: (
      recordingId: string,
      blob: Blob,
    ) => Promise<MeetingRecordingUploadResponse>
    onTrackError?: (error: Error, track: RecordedParticipantTrack) => void
  },
): Promise<Blob | null> {
  const result = await stopBrowserRecorder()
  let blob: Blob | null = null
  let tracks: readonly RecordedParticipantTrack[] = []

  if (result && typeof result === 'object' && 'blob' in result) {
    blob = result.blob
    tracks = result.tracks ?? []
  } else {
    blob = result as Blob | null
  }

  await stopBackend()
  if (!blob || blob.size === 0) return null

  onUploadState('uploading')
  try {
    const response = await uploadBlob(blob)
    onUploadState(getRecordingUploadState(response.status))
  } catch (error) {
    onUploadState('error')
    throw error
  }

  if (participantTrackOptions && participantTrackOptions.registerTracks && participantTrackOptions.uploadTrack) {
    const resolvedTracks = participantTrackOptions.getTracks
      ? await participantTrackOptions.getTracks()
      : tracks

    if (resolvedTracks && resolvedTracks.length > 0) {
      await uploadParticipantTracks({
        tracks: resolvedTracks,
        registerTracks: participantTrackOptions.registerTracks,
        uploadTrack: participantTrackOptions.uploadTrack,
        onTrackError: participantTrackOptions.onTrackError,
      })
    }
  }

  return blob
}
