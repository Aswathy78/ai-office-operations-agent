export interface Meeting {
  id: string
  title: string
  startTime: string
  endTime: string
  startAt?: string
  participants?: number
  status: string
  joinUrl: string
  agenda?: string | null
  organizer?: string | null
  organizerId?: string | null
  lifecycle?: 'active' | 'upcoming' | 'completed'
}

export interface MeetingListRecord {
  id: string
  title: string
  agenda: string | null
  start_at: string
  end_at: string
  join_url: string
  status: string
  organizer_id?: string | null
}

export interface MeetingDetails {
  id: string
  title: string
  agenda: string | null
  start_at: string
  end_at: string
  status: string
  organizer_id?: string | null
  is_valid: boolean
  is_expired: boolean
}

export interface GuestJoinRequest {
  guest_name: string
  guest_email?: string
}

export interface GuestRequest {
  id: string
  meeting_id: string
  guest_name: string
  guest_email: string | null
  status: 'pending' | 'admitted' | 'rejected'
  requested_at: string
  decided_at: string | null
  attendance_log_id: string | null
}

export interface GuestDecision {
  decision: 'admit' | 'reject'
}

export type GuestLobbyState =
  | { status: 'loading' }
  | { status: 'form'; apiError?: string }
  | { status: 'submitting' }
  | {
      status: 'waiting'
      request: GuestRequest
      connection: 'connecting' | 'connected' | 'disconnected'
      socketError?: string
    }
  | { status: 'admitted'; request: GuestRequest }
  | { status: 'rejected'; request: GuestRequest }
  | { status: 'error'; message: string }

export type GuestLobbyWebSocketMessage =
  | { type: 'lobby_welcome'; payload?: Record<string, unknown> }
  | { type: 'welcome'; payload?: Record<string, unknown> }
  | { type: 'guest_arrived'; payload?: Record<string, unknown> }
  | { type: 'guest_request_notify'; payload?: Record<string, unknown> }
  | {
      type: 'guest_admitted' | 'guest_rejected'
      guest_request_id?: string
      meeting_id?: string
      guest_name?: string
      status?: string
      decided_at?: string | null
      payload?: Record<string, unknown>
    }
  | { type: 'error'; payload?: Record<string, unknown> }

export type MeetingConnectionState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'error'

export type MeetingMediaState =
  | 'idle'
  | 'requesting'
  | 'ready'
  | 'permission_denied'
  | 'device_error'
  | 'unavailable'
  | 'stopped'

export interface ParticipantPeerState {
  participantId: string
  connectionState: RTCPeerConnectionState
  iceConnectionState: RTCIceConnectionState
  signalingState: RTCSignalingState
}

/** Metadata broadcasted by each participant for display in the roster */
export interface ParticipantInfo {
  connectionId: string
  displayName: string
  email?: string | null
  isOrganizer: boolean
  isMicrophoneEnabled: boolean | null
  isCameraEnabled: boolean | null
}

export type MeetingSignalType = 'offer' | 'answer' | 'ice_candidate'

export type MeetingWebSocketMessage =
  | {
      type: 'welcome'
      sender_id: string
      target_id?: string
      payload: {
        connection_id: string
        meeting_id: string
        participants: string[]
        organizer_id?: string
      }
    }
  | {
      type: 'participant_joined' | 'participant_left'
      sender_id: string
      payload: { connection_id: string }
    }
  | {
      type: 'join_ack'
      sender_id: string
      target_id?: string
      payload: { connection_id: string; meeting_id: string }
    }
  | {
      type: MeetingSignalType
      sender_id: string
      target_id: string
      payload: Record<string, unknown>
    }
  | {
      type: 'guest_arrived' | 'guest_request_notify'
      sender_id: string
      payload: Record<string, unknown>
    }
  | {
      type: 'mute_participant'
      sender_id: string
      target_id: string
      payload: { participant_id: string; by_organizer: boolean; meeting_id: string }
    }
  | {
      type: 'ask_unmute'
      sender_id: string
      target_id: string
      payload: { participant_id: string; meeting_id: string }
    }
  | {
      type: 'meeting_ended'
      sender_id: string
      payload: { meeting_id: string; reason: string }
    }
  | {
      type: 'participant_state'
      sender_id: string
      payload: {
        connection_id: string
        display_name?: string
        is_microphone_enabled?: boolean | null
        is_camera_enabled?: boolean | null
        is_organizer?: boolean
      }
    }
  | {
      type: 'error'
      sender_id: string
      target_id?: string
      payload: { detail: string }
    }

export type MeetingClientMessage =
  | { type: 'join' }
  | { type: 'leave' }
  | {
      type: MeetingSignalType
      target_id: string
      payload: Record<string, unknown>
    }
  | {
      type: 'mute_participant'
      payload: { participant_id: string }
    }
  | {
      type: 'ask_unmute'
      payload: { participant_id: string }
    }
  | {
      type: 'participant_state'
      payload: {
        connection_id: string
        display_name?: string
        is_microphone_enabled?: boolean | null
        is_camera_enabled?: boolean | null
        is_organizer?: boolean
      }
    }

// Meeting Report
export interface AttendanceReportRow {
  participant_name: string
  participant_email: string | null
  user_id: string | null
  is_external_guest: boolean
  joined_at: string
  left_at: string | null
}

export interface MeetingReport {
  meeting_id: string
  title: string
  agenda: string | null
  start_at: string
  end_at: string
  organizer_id: string
  attendance: AttendanceReportRow[]
}
