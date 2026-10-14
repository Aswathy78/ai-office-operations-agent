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
  organizer_id: string
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

export interface MeetingSummary {
  id: string
  meeting_id: string
  transcript_id: string
  status: 'pending' | 'processing' | 'completed' | 'failed'
  overview: string | null
  decisions: string[] | null
  discussion_topics: string[] | null
  open_questions: string[] | null
  created_at: string
  updated_at: string
  completed_at: string | null
  error_message: string | null
}

export type MeetingActionItemExtractionStatus =
  | 'pending'
  | 'processing'
  | 'completed'
  | 'failed'

export type MeetingActionItemPriority = 'low' | 'medium' | 'high' | 'urgent'

export interface Employee {
  id: string
  name: string
  email: string
  team?: string | null
}

export type EmployeeMatchStatus =
  | 'unique_match'
  | 'ambiguous_match'
  | 'no_match'
  | 'missing_owner'

export type MeetingActionItemOwnershipStatus =
  | 'unresolved'
  | 'proposed'
  | 'approval_required'
  | 'confirmed'

export type MeetingActionItemReviewStatus =
  | 'pending_review'
  | 'approval_required'
  | 'confirmed'
  | 'rejected'

export interface MeetingActionItem {
  id: string
  owner_name: string | null
  proposed_employee_id?: string | null
  confirmed_employee_id?: string | null
  employee_match_status?: EmployeeMatchStatus | null
  ownership_status?: MeetingActionItemOwnershipStatus | null
  review_status?: MeetingActionItemReviewStatus | null
  reviewed_by?: string | null
  reviewed_at?: string | null
  is_task_eligible?: boolean
  requires_approval?: boolean
  task_description: string
  deadline_at: string | null
  deadline_text: string | null
  priority: MeetingActionItemPriority
  confidence: number
  meeting_id: string
  transcript_id: string
  source_segment_index: number
  source_start_seconds: number
  source_end_seconds: number
  source_quote_start_seconds: number | null
  source_quote_end_seconds: number | null
  source_quote: string | null
}

export interface MeetingActionItems {
  id: string
  meeting_id: string
  transcript_id: string
  status: MeetingActionItemExtractionStatus
  created_at: string
  updated_at: string
  processing_started_at: string | null
  completed_at: string | null
  error_message: string | null
  action_items: MeetingActionItem[]
}

export interface TranscriptSegment {
  start: number
  end: number
  text: string
  speaker: string
  flagged_for_correction: boolean
  words?: TranscriptWord[] | null
}

export interface TranscriptWord {
  start: number
  end: number
  word: string
}

export interface MeetingTranscript {
  id: string
  meeting_id: string
  recording_id: string
  status: string
  language: string
  transcript_text: string | null
  segments: TranscriptSegment[]
  error_message: string | null
  created_at: string
  processing_started_at: string | null
  completed_at: string | null
}

export interface TranscriptSegmentUpdate {
  text: string
  speaker?: string
}

export interface MeetingTranscriptUpdateRequest {
  segments: TranscriptSegmentUpdate[]
}

export interface TranscriptCorrection {
  id: string
  meeting_id: string
  transcript_id: string
  segment_index: number
  original_text: string
  corrected_text: string
  original_speaker: string
  corrected_speaker: string
  editor_id: string
  created_at: string
}

// Meeting Minutes (Story 4.2 / FR-19)
export type MeetingMinutesDeliveryStatus = 'pending' | 'sent' | 'failed'
export type MeetingMinutesStatus = 'draft' | 'pending_approval' | 'approved' | 'sent' | 'failed'
export type MeetingMinutesPolicy = 'manual_approval' | 'auto_send'

export interface MeetingMinutesRecipient {
  id: string
  minutes_id: string
  recipient_name: string
  recipient_email: string
  attendance_log_id: string | null
  delivery_status: MeetingMinutesDeliveryStatus
  error_message: string | null
  idempotency_key: string | null
  sent_at: string | null
  created_at: string
}

export interface MeetingMinutesContentActionItem {
  id?: string
  task_description: string
  owner_name: string | null
  owner_email?: string | null
  deadline_at: string | null
  deadline_text: string | null
  priority: string
}

export interface MeetingMinutes {
  id: string
  meeting_id: string
  summary_id: string | null
  status: MeetingMinutesStatus
  policy: MeetingMinutesPolicy
  approved_by: string | null
  approved_at: string | null
  content_summary: string | null
  content_decisions: string[] | null
  content_action_items: MeetingMinutesContentActionItem[] | null
  error_message: string | null
  created_at: string
  updated_at: string
  recipients: MeetingMinutesRecipient[]
}

export interface MeetingMinutesApproveRequest {
  notes?: string | null
}

export interface MeetingMinutesDeliveryResponse {
  minutes_id: string
  meeting_id: string
  total: number
  sent: number
  failed: number
  pending: number
  recipients: MeetingMinutesRecipient[]
}

export interface MeetingMinutesUpdateRequest {
  content_summary?: string | null
  content_decisions?: string[] | null
  content_action_items?: MeetingMinutesContentActionItem[] | null
}

