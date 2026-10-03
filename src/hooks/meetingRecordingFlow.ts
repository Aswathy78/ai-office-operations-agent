import type {
  MeetingRecordingRecord,
  MeetingRecordingUploadResponse,
  RecordingConsentRecord,
  RecordingConsentStatus,
} from '../api/meetings'

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

export async function stopRecordingAndUpload(
  stopBrowserRecorder: () => Promise<Blob | null>,
  stopBackend: () => Promise<MeetingRecordingRecord>,
  uploadBlob: (blob: Blob) => Promise<MeetingRecordingUploadResponse>,
  onUploadState: (state: RecordingUploadState) => void,
): Promise<Blob | null> {
  const blob = await stopBrowserRecorder()
  await stopBackend()
  if (!blob || blob.size === 0) return null

  onUploadState('uploading')
  try {
    const response = await uploadBlob(blob)
    onUploadState(getRecordingUploadState(response.status))
    return blob
  } catch (error) {
    onUploadState('error')
    throw error
  }
}