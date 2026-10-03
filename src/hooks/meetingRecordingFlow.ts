import type {
  MeetingRecordingRecord,
  RecordingConsentRecord,
  RecordingConsentStatus,
} from '../api/meetings'

export type RecordingUploadState = 'uploading' | 'uploaded' | 'error'

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
  uploadBlob: (blob: Blob) => Promise<unknown>,
  onUploadState: (state: RecordingUploadState) => void,
): Promise<Blob | null> {
  const blob = await stopBrowserRecorder()
  await stopBackend()
  if (!blob || blob.size === 0) return null

  onUploadState('uploading')
  try {
    await uploadBlob(blob)
    onUploadState('uploaded')
    return blob
  } catch (error) {
    onUploadState('error')
    throw error
  }
}