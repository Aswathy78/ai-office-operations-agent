import type {
  GuestDecision,
  GuestJoinRequest,
  GuestRequest,
  Employee,
  MeetingActionItem,
  MeetingActionItemPriority,
  MeetingActionItemReviewStatus,
  MeetingActionItems,
  MeetingDetails,
  MeetingListRecord,
  MeetingReport,
  MeetingSummary,
  MeetingTranscript,
  MeetingTranscriptUpdateRequest,
  TranscriptCorrection,
  TranscriptSegmentUpdate,
} from '../types/meeting'

const API_BASE_URL = 'http://127.0.0.1:9000'

export class MeetingApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'MeetingApiError'
  }
}

async function readResponse<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body &&
      typeof body.detail === 'string'
        ? body.detail
        : `Request failed (${response.status})`
    throw new MeetingApiError(detail, response.status)
  }
  return body as T
}

export function getMeeting(meetingId: string): Promise<MeetingDetails> {
  return fetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}`)
    .then((response) => readResponse<MeetingDetails>(response))
}

export async function getMeetings(userId: string, userEmail: string): Promise<MeetingListRecord[]> {
  const query = new URLSearchParams({ user_id: userId, email: userEmail })
  return fetch(`${API_BASE_URL}/api/v1/meetings/?${query}`)
    .then((response) => readResponse<MeetingListRecord[]>(response))
}

export function requestGuestJoin(
  meetingId: string,
  request: GuestJoinRequest,
): Promise<GuestRequest> {
  return fetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/lobby`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }).then((response) => readResponse<GuestRequest>(response))
}

export function getPendingGuestRequests(meetingId: string): Promise<GuestRequest[]> {
  return fetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/lobby`)
    .then((response) => readResponse<GuestRequest[]>(response))
}

export function decideGuestRequest(
  meetingId: string,
  requestId: string,
  decision: GuestDecision,
  organizerId: string,
): Promise<GuestRequest> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/lobby/${encodeURIComponent(requestId)}/decision?${query}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(decision),
    },
  ).then((response) => readResponse<GuestRequest>(response))
}

export function getMeetingReport(meetingId: string, organizerId: string): Promise<MeetingReport> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/report?${query}`)
    .then((response) => readResponse<MeetingReport>(response))
}

export function getMeetingSummary(
  meetingId: string,
  organizerId: string,
): Promise<MeetingSummary> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/summary?${query}`,
  ).then((response) => readResponse<MeetingSummary>(response))
}

export function getMeetingActionItems(
  meetingId: string,
  organizerId: string,
): Promise<MeetingActionItems> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/action-items?${query}`,
  ).then((response) => readResponse<MeetingActionItems>(response))
}

export function getActiveEmployees(): Promise<Employee[]> {
  return fetch(`${API_BASE_URL}/api/v1/meetings/employees`)
    .then((response) => readResponse<Employee[]>(response))
}

export function confirmActionItemOwner(
  meetingId: string,
  actionItemId: string,
  employeeId: string,
  organizerId: string,
): Promise<MeetingActionItem> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/action-items/${encodeURIComponent(actionItemId)}/confirm-owner?${query}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ employee_id: employeeId }),
    },
  ).then((response) => readResponse<MeetingActionItem>(response))
}

export interface ActionItemReviewPayload {
  action?: 'confirm' | 'reject' | 'edit' | 'save'
  decision?: 'confirm' | 'reject' | 'edit' | 'confirmed' | 'rejected'
  review_status?: MeetingActionItemReviewStatus
  task_description?: string
  owner_name?: string | null
  confirmed_employee_id?: string | null
  deadline_at?: string | null
  deadline_text?: string | null
  priority?: MeetingActionItemPriority
}

export function reviewActionItem(
  meetingId: string,
  actionItemId: string,
  payload: ActionItemReviewPayload,
  organizerId: string,
): Promise<MeetingActionItem> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/action-items/${encodeURIComponent(actionItemId)}/review?${query}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  ).then((response) => readResponse<MeetingActionItem>(response))
}

export function extractMeetingActionItems(
  meetingId: string,
  organizerId: string,
): Promise<MeetingActionItems> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/action-items/extract?${query}`,
    { method: 'POST' },
  ).then((response) => readResponse<MeetingActionItems>(response))
}

export function getMeetingTranscript(
  meetingId: string,
  organizerId: string,
  transcriptId?: string,
): Promise<MeetingTranscript> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  if (transcriptId) query.set('transcript_id', transcriptId)
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/transcript?${query}`,
  ).then((response) => readResponse<MeetingTranscript>(response))
}

export function updateMeetingTranscript(
  meetingId: string,
  organizerId: string,
  segments: TranscriptSegmentUpdate[],
  transcriptId?: string,
): Promise<MeetingTranscript> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  if (transcriptId) query.set('transcript_id', transcriptId)
  const body: MeetingTranscriptUpdateRequest = {
    segments: segments.map(({ text, speaker }) => ({
      text,
      ...(speaker === undefined ? {} : { speaker }),
    })),
  }
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/transcript?${query}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  ).then((response) => readResponse<MeetingTranscript>(response))
}

export function getMeetingTranscriptCorrections(
  meetingId: string,
  organizerId: string,
  transcriptId?: string,
): Promise<TranscriptCorrection[]> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  if (transcriptId) query.set('transcript_id', transcriptId)
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/transcript/corrections?${query}`,
  ).then((response) => readResponse<TranscriptCorrection[]>(response))
}

export function meetingWebSocketUrl(meetingId: string, userId?: string | null): string {
  const url = new URL(
    `/api/v1/meetings/${encodeURIComponent(meetingId)}/ws`,
    API_BASE_URL,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (userId) url.searchParams.set('user_id', userId)
  return url.toString()
}

export function guestLobbyWebSocketUrl(meetingId: string, requestId: string): string {
  const url = new URL(
    `/api/v1/meetings/${encodeURIComponent(meetingId)}/lobby-ws`,
    API_BASE_URL,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('guest_request_id', requestId)
  return url.toString()
}

export interface AttendanceJoinRequest {
  participant_name: string
  participant_email?: string | null
  user_id?: string | null
  is_external_guest?: boolean
  guest_request_id?: string | null
}

export interface AttendanceRecord {
  id: string
  meeting_id: string
  participant_name: string
  participant_email: string | null
  user_id: string | null
  is_external_guest: boolean
  joined_at: string
  left_at: string | null
}

export type RecordingConsentStatus = 'consented' | 'declined'

export interface RecordingConsentRecord {
  id: string
  meeting_id: string
  attendance_id: string
  consent_status: RecordingConsentStatus
  consented_at: string | null
  revoked_at: string | null
  created_at: string
}

export interface MeetingRecordingRecord {
  id: string
  meeting_id: string
  participant_id: string | null
  status: 'pending' | 'recording' | 'processing' | 'completed' | 'failed' | 'cancelled'
  started_at: string | null
  completed_at: string | null
  created_at: string
  retention_expires_at: string
  storage_uri: string | null
  error_message: string | null
}

export interface MeetingRecordingUploadResponse {
  recording_id: string
  status: MeetingRecordingRecord['status']
  storage_uri: string
  filename: string
  content_type: string
  size_bytes: number
}

export function recordMeetingRecordingConsent(
  meetingId: string,
  attendanceId: string,
  consentStatus: RecordingConsentStatus,
): Promise<RecordingConsentRecord> {
  return fetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/consent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ attendance_id: attendanceId, consent_status: consentStatus }),
  }).then((response) => readResponse<RecordingConsentRecord>(response))
}

export function startMeetingRecording(
  meetingId: string,
  organizerId: string,
): Promise<MeetingRecordingRecord> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/start?${query}`,
    { method: 'POST' },
  ).then((response) => readResponse<MeetingRecordingRecord>(response))
}

export function stopMeetingRecording(
  meetingId: string,
  organizerId: string,
): Promise<MeetingRecordingRecord> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/stop?${query}`,
    { method: 'POST' },
  ).then((response) => readResponse<MeetingRecordingRecord>(response))
}

export function uploadMeetingRecording(
  meetingId: string,
  organizerId: string,
  recording: Blob,
): Promise<MeetingRecordingUploadResponse> {
  const mimeType = recording.type.split(';', 1)[0].trim().toLowerCase()
  const extensionByMimeType: Record<string, string> = {
    'audio/webm': 'webm',
    'video/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/mp4': 'm4a',
    'audio/mpeg': 'mp3',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
  }
  const extension = extensionByMimeType[mimeType]
  if (!extension) {
    throw new MeetingApiError('The completed recording has an unsupported audio format.', 415)
  }

  const query = new URLSearchParams({ organizer_id: organizerId })
  const formData = new FormData()
  formData.append('file', recording, `recording.${extension}`)
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/upload?${query}`,
    { method: 'POST', body: formData },
  ).then((response) => readResponse<MeetingRecordingUploadResponse>(response))
}

export interface ParticipantTrackRegistration {
  connection_id: string
  participant_name: string
  attendance_id?: string | null
}

export interface ParticipantTracksRegisterRequest {
  tracks: ParticipantTrackRegistration[]
}

export interface ParticipantTrackRegistrationResponse {
  recording_id: string
  connection_id: string
  participant_name: string
  status: string
  meeting_id: string
}

export function registerParticipantTracks(
  meetingId: string,
  organizerId: string,
  tracks: ParticipantTrackRegistration[],
): Promise<ParticipantTrackRegistrationResponse[]> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/tracks?${query}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tracks }),
    },
  ).then((response) => readResponse<ParticipantTrackRegistrationResponse[]>(response))
}

export function uploadParticipantTrack(
  meetingId: string,
  recordingId: string,
  organizerId: string,
  recording: Blob,
): Promise<MeetingRecordingUploadResponse> {
  const mimeType = recording.type.split(';', 1)[0].trim().toLowerCase()
  const extensionByMimeType: Record<string, string> = {
    'audio/webm': 'webm',
    'video/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/mp4': 'm4a',
    'audio/mpeg': 'mp3',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
  }
  const extension = extensionByMimeType[mimeType] ?? 'webm'

  const query = new URLSearchParams({ organizer_id: organizerId })
  const formData = new FormData()
  formData.append('file', recording, `participant_track.${extension}`)
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/${encodeURIComponent(recordingId)}/upload?${query}`,
    { method: 'POST', body: formData },
  ).then((response) => readResponse<MeetingRecordingUploadResponse>(response))
}


export function joinMeetingAttendance(
  meetingId: string,
  participant: AttendanceJoinRequest,
): Promise<AttendanceRecord> {
  return fetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/attendance/join`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(participant),
  }).then((response) => readResponse<AttendanceRecord>(response))
}

export function leaveMeetingAttendance(meetingId: string, attendanceId: string): Promise<AttendanceRecord> {
  return fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/attendance/${encodeURIComponent(attendanceId)}/leave`,
    { method: 'POST', keepalive: true },
  ).then((response) => readResponse<AttendanceRecord>(response))
}

export async function createMeeting(
  data: {
    title: string
    agenda?: string | null
    meeting_type: string
    start_at: string
    end_at: string
    participants: { email?: string; user_id?: string }[]
  },
  organizerId: string,
): Promise<{ id: string; title: string; join_url: string; status: string; start_at: string; end_at: string; organizer_id?: string | null }> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  const response = await fetch(`${API_BASE_URL}/api/v1/meetings/?${query}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return readResponse(response)
}

export async function deleteMeeting(meetingId: string, organizerId: string): Promise<void> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  const response = await fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}?${query}`,
    { method: 'DELETE' },
  )
  if (!response.ok && response.status !== 204) {
    await readResponse<void>(response)
  }
}

export async function endMeetingForEveryone(
  meetingId: string,
  organizerId: string,
): Promise<{ id: string; status: string }> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  const response = await fetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/end?${query}`,
    { method: 'POST' },
  )
  return readResponse(response)
}