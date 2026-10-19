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
  MeetingParticipantRecord,
  MeetingChangeResult,
  MeetingParticipantBulkChangeResult,
  MeetingCancellationRequest,
  MeetingCancellationDecisionResult,
  MeetingAuditEntry,
  ScheduleResult,
  ScheduledMeetingDetail,
  ConflictDetail,
  MeetingRescheduleRequest,
  RescheduleDecisionResponse,
} from '../types/meeting'
import { API_BASE_URL, apiFetch } from './client'
export { API_BASE_URL } from './client'

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

export async function getMeetings(): Promise<MeetingListRecord[]> {
  return apiFetch(`${API_BASE_URL}/api/v1/meetings/`)
    .then((response) => readResponse<MeetingListRecord[]>(response))
}

export function getCalendarMeetings(
  year: number,
  month: number,
): Promise<MeetingCalendarResponse> {
  const query = new URLSearchParams({
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
  return apiFetch(
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
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/summary?${query}`,
  ).then((response) => readResponse<MeetingSummary>(response))
}

export function getMeetingActionItems(
  meetingId: string,
  organizerId: string,
): Promise<MeetingActionItems> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
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
  return apiFetch(
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

export function meetingWebSocketUrl(meetingId: string): string {
  const url = new URL(
    `/api/v1/meetings/${encodeURIComponent(meetingId)}/ws`,
    API_BASE_URL,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
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
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/recording/start?${query}`,
    { method: 'POST' },
  ).then((response) => readResponse<MeetingRecordingRecord>(response))
}

export function stopMeetingRecording(
  meetingId: string,
  organizerId: string,
): Promise<MeetingRecordingRecord> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  return apiFetch(
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
  return apiFetch(
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
  return apiFetch(
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
  return apiFetch(
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
  return apiFetch(
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
): Promise<{
  id: string
  title: string
  join_url: string
  status: string
  start_at: string
  end_at: string
  organizer_id?: string | null
  timezone: string
  invitations_sent: string[]
  invitation_failures: string[]
}> {
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
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}?${query}`,
    { method: 'DELETE' },
  )
  if (!response.ok && response.status !== 204) {
    await readResponse<void>(response)
  }
}

export function getMeetingParticipants(
  meetingId: string,
): Promise<MeetingParticipantRecord[]> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/participants`,
  ).then((response) => readResponse<MeetingParticipantRecord[]>(response))
}

export function getMeetingAudit(meetingId: string): Promise<MeetingAuditEntry[]> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/audit`,
  ).then((response) => readResponse<MeetingAuditEntry[]>(response))
}

export function addMeetingParticipant(
  meetingId: string,
  participant: { user_id?: string; email?: string },
): Promise<MeetingChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/participants`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(participant),
    },
  ).then((response) => readResponse<MeetingChangeResult>(response))
}

export function addMeetingParticipants(
  meetingId: string,
  emails: string[],
): Promise<MeetingParticipantBulkChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/participants/bulk`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails }),
    },
  ).then((response) => readResponse<MeetingParticipantBulkChangeResult>(response))
}

export function removeMeetingParticipant(
  meetingId: string,
  participantId: string,
): Promise<MeetingChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/participants/${encodeURIComponent(participantId)}`,
    { method: 'DELETE' },
  ).then((response) => readResponse<MeetingChangeResult>(response))
}

export function cancelMeeting(
  meetingId: string,
  reason?: string,
): Promise<MeetingChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/${encodeURIComponent(meetingId)}/cancel`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason || null }),
    },
  ).then((response) => readResponse<MeetingChangeResult>(response))
}

export function getPendingMeetingCancellations(): Promise<MeetingCancellationRequest[]> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/cancellation-requests`,
  ).then((response) => readResponse<MeetingCancellationRequest[]>(response))
}

export function decideMeetingCancellation(
  requestId: string,
  decision: 'approve' | 'reject',
  note?: string,
): Promise<MeetingCancellationDecisionResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/cancellation-requests/${encodeURIComponent(requestId)}/decide`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, note: note || null }),
    },
  ).then((response) => readResponse<MeetingCancellationDecisionResult>(response))
}

export async function endMeetingForEveryone(
  meetingId: string,
  organizerId: string,
): Promise<{ id: string; status: string }> {
  const query = new URLSearchParams({ organizer_id: organizerId })
  const response = await apiFetch(
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

// ── Story 5.3: Meeting Reschedule Approvals ──────────────────────────────

/**
 * GET /api/v1/meetings/reschedule-requests
 * Returns reschedule requests visible to the authenticated user.
 * Organizers see requests for their meetings; others see their own requests.
 */
export function getRescheduleRequests(
  actor: { id: string },
  statusFilter?: 'pending' | '',
): Promise<MeetingRescheduleRequest[]> {
  const query = new URLSearchParams()
  if (statusFilter) query.set('status', statusFilter)
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/reschedule-requests${query.toString() ? `?${query}` : ''}`,
    { headers: { 'X-User-Id': actor.id } },
  ).then((response) => readResponse<MeetingRescheduleRequest[]>(response))
}

/**
 * POST /api/v1/meetings/reschedule-requests/{request_id}/decide
 * Approve or reject a pending reschedule request (organizer only).
 */
export function decideRescheduleRequest(
  requestId: string,
  decision: 'approve' | 'reject',
  note: string,
  actor: { id: string },
): Promise<RescheduleDecisionResponse> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/meetings/reschedule-requests/${encodeURIComponent(requestId)}/decide`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Id': actor.id,
      },
      body: JSON.stringify({ decision, note: note || null }),
    },
  ).then((response) => readResponse<RescheduleDecisionResponse>(response))
}
