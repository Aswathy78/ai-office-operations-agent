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
  MeetingCalendarResponse,
  MeetingReport,
  MeetingSummary,
  MeetingTranscript,
  MeetingTranscriptUpdateRequest,
  TranscriptCorrection,
  TranscriptSegmentUpdate,
  MeetingMinutes,
  MeetingMinutesApproveRequest,
  MeetingMinutesDeliveryResponse,
  MeetingMinutesRecipient,
  MeetingMinutesUpdateRequest,
  ScheduleResult,
  ScheduledMeetingDetail,
  ConflictDetail,
} from '../types/meeting'

export let API_BASE_URL =
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_BASE_URL) ||
  'http://127.0.0.1:8000'

function getFallbackBaseUrl(current: string): string | null {
  if (current.includes(':8000')) {
    return current.replace(':8000', ':9000')
  }
  if (current.includes(':9000')) {
    return current.replace(':9000', ':8000')
  }
  return null
}

async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const urlStr = typeof input === 'string' ? input : input.toString()
  try {
    return init !== undefined ? await fetch(urlStr, init) : await fetch(urlStr)
  } catch (err) {
    const fallback = getFallbackBaseUrl(API_BASE_URL)
    if (fallback && urlStr.startsWith(API_BASE_URL)) {
      const fallbackUrl = urlStr.replace(API_BASE_URL, fallback)
      try {
        const response =
          init !== undefined ? await fetch(fallbackUrl, init) : await fetch(fallbackUrl)
        API_BASE_URL = fallback
        return response
      } catch {
        // preserve original error
      }
    }
    throw err
  }
}

export class MeetingApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'MeetingApiError'
  }
}

async function readResponse<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const rawDetail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? body.detail
        : null
    const detail = typeof rawDetail === 'string'
      ? rawDetail
      : typeof rawDetail === 'object' && rawDetail !== null
        ? [
            'message' in rawDetail && typeof rawDetail.message === 'string' ? rawDetail.message : null,
            'conflicts' in rawDetail && Array.isArray(rawDetail.conflicts)
              ? rawDetail.conflicts.map((conflict) => {
                  if (typeof conflict !== 'object' || conflict === null) return null
                  const name = 'attendee_name' in conflict && typeof conflict.attendee_name === 'string'
                    ? conflict.attendee_name
                    : null
                  const email = 'attendee_email' in conflict && typeof conflict.attendee_email === 'string'
                    ? conflict.attendee_email
                    : null
                  return [name, email].filter(Boolean).join(' — ')
                }).filter(Boolean).join(', ')
              : null,
          ].filter(Boolean).join(' ')
        : `Request failed (${response.status})`
    throw new MeetingApiError(detail, response.status)
  }
  return body as T
}

export function getMeeting(meetingId: string): Promise<MeetingDetails> {
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}`)
    .then((response) => readResponse<MeetingDetails>(response))
}

export async function getMeetings(userId: string, userEmail: string): Promise<MeetingListRecord[]> {
  const query = new URLSearchParams({ user_id: userId, email: userEmail })
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/?${query}`)
    .then((response) => readResponse<MeetingListRecord[]>(response))
}

export function getCalendarMeetings(
  userId: string,
  userEmail: string,
  year: number,
  month: number,
): Promise<MeetingCalendarResponse> {
  const query = new URLSearchParams({
    user_id: userId,
    email: userEmail,
    year: String(year),
    month: String(month),
  })
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/calendar?${query}`)
    .then((response) => readResponse<MeetingCalendarResponse>(response))
}

export function requestGuestJoin(
  meetingId: string,
  request: GuestJoinRequest,
): Promise<GuestRequest> {
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/lobby`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }).then((response) => readResponse<GuestRequest>(response))
}

export function getPendingGuestRequests(meetingId: string): Promise<GuestRequest[]> {
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/lobby`)
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

export function getMeetingReport(
  meetingId: string,
  organizerId: string,
  employeeEmail?: string,
): Promise<MeetingReport> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  if (employeeEmail) query.set('employee_email', employeeEmail)
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/report?${query}`)
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
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/employees`)
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
  return apiFetch(
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
  options: { retryEmpty?: boolean } = {},
): Promise<MeetingActionItems> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  // Re-runs an extraction that completed without finding any action items.
  if (options.retryEmpty) query.set('retry_empty', 'true')
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/action-items/extract?${query}`,
    { method: 'POST' },
  ).then((response) => readResponse<MeetingActionItems>(response))
}

export function getMeetingTranscript(
  meetingId: string,
  organizerId: string,
  transcriptId?: string,
  employeeEmail?: string,
): Promise<MeetingTranscript> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  if (transcriptId) query.set('transcript_id', transcriptId)
  if (employeeEmail) query.set('employee_email', employeeEmail)
  return apiFetch(
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
  return apiFetch(
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
  return apiFetch(
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
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/consent`, {
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
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/attendance/join`, {
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
    start_at?: string
    end_at?: string
    participants: { email?: string; user_id?: string }[]
  },
  organizerId: string,
): Promise<{ id: string; title: string; join_url: string; status: string; start_at: string; end_at: string; organizer_id?: string | null; timezone: string }> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  const response = await apiFetch(`${API_BASE_URL}/api/v1/meetings/?${query}`, {
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

export function getMeetingMinutes(
  meetingId: string,
  organizerId: string,
): Promise<MeetingMinutes> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes?${query}`,
    {
      headers: {
        'X-User-Id': organizerId,
      },
    },
  ).then((response) => readResponse<MeetingMinutes>(response))
}

export function generateMeetingMinutes(
  meetingId: string,
  organizerId: string,
): Promise<MeetingMinutes> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes/generate?${query}`,
    {
      method: 'POST',
      headers: {
        'X-User-Id': organizerId,
      },
    },
  ).then((response) => readResponse<MeetingMinutes>(response))
}

export function approveMeetingMinutes(
  meetingId: string,
  organizerId: string,
  payload?: MeetingMinutesApproveRequest,
): Promise<MeetingMinutes> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes/approve?${query}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Id': organizerId,
      },
      body: JSON.stringify(payload ?? {}),
    },
  ).then((response) => readResponse<MeetingMinutes>(response))
}

export function sendMeetingMinutes(
  meetingId: string,
  organizerId: string,
  retryFailed: boolean = false,
): Promise<MeetingMinutesDeliveryResponse> {
  const query = new URLSearchParams({
    organizer_id: organizerId,
    retry_failed: String(retryFailed),
  })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes/send?${query}`,
    {
      method: 'POST',
      headers: {
        'X-User-Id': organizerId,
      },
    },
  ).then((response) => readResponse<MeetingMinutesDeliveryResponse>(response))
}

export function getMeetingMinutesRecipients(
  meetingId: string,
  organizerId: string,
): Promise<MeetingMinutesRecipient[]> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes/recipients?${query}`,
    {
      headers: {
        'X-User-Id': organizerId,
      },
    },
  ).then((response) => readResponse<MeetingMinutesRecipient[]>(response))
}

export function updateMeetingMinutes(
  meetingId: string,
  organizerId: string,
  payload: MeetingMinutesUpdateRequest,
): Promise<MeetingMinutes> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes?${query}`,
    {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Id': organizerId,
      },
      body: JSON.stringify(payload),
    },
  ).then((response) => readResponse<MeetingMinutes>(response))
}

export function refreshMeetingMinutes(
  meetingId: string,
  organizerId: string,
): Promise<MeetingMinutes> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/minutes/refresh?${query}`,
    {
      method: 'POST',
      headers: {
        'X-User-Id': organizerId,
      },
    },
  ).then((response) => readResponse<MeetingMinutes>(response))
}

export function scheduleMeetingNL(
  prompt: string,
  actor: { id: string; role?: string },
): Promise<ScheduleResult> {
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/schedule/natural-language`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-Id': actor.id,
    },
    body: JSON.stringify({
      prompt,
      organizer_id: actor.id,
    }),
  }).then((response) => readResponse<ScheduleResult>(response))
}

export function confirmMeetingSlot(
  prompt: string,
  startAt: string,
  actor: { id: string; role?: string },
): Promise<ScheduleResult> {
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/schedule/confirm`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-Id': actor.id,
    },
    body: JSON.stringify({
      prompt,
      start_at: startAt,
      organizer_id: actor.id,
    }),
  }).then((response) => readResponse<ScheduleResult>(response))
}
