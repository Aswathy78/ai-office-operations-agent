import type {
  GuestDecision,
  GuestJoinRequest,
  GuestRequest,
  MeetingDetails,
  MeetingListRecord,
  MeetingReport,
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