// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { getMeeting, getMeetingSummary } from '../api/meetings'
import { MeetingRoomPage } from './MeetingRoomPage'
import type { MeetingDetails, MeetingSummary } from '../types/meeting'

const { authValue, roomValue } = vi.hoisted(() => ({
  authValue: { user: { id: 'organizer-1', name: 'Maya', email: 'maya@example.com' } },
  roomValue: {
    isMutedByOrganizer: false,
    isAskedToUnmute: false,
    meetingEnded: false,
    localStream: null,
    mediaState: 'idle',
    mediaError: null,
    startLocalMedia: vi.fn(),
    stopLocalMedia: vi.fn(),
    isMicrophoneEnabled: false,
    isCameraEnabled: false,
    toggleMicrophone: vi.fn(),
    toggleCamera: vi.fn(),
    remoteStreams: new Map(),
    participantInfos: new Map(),
    participants: [],
    peerStates: new Map(),
    localParticipantId: null,
    connectionState: 'idle',
    lastMessage: null,
    organizerId: null,
    isOrganizer: false,
    disconnect: vi.fn(),
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/meetings', () => ({
  MeetingApiError: class MeetingApiError extends Error {
    constructor(message: string, readonly status: number) {
      super(message)
    }
  },
  getMeeting: vi.fn(),
  getMeetingSummary: vi.fn(),
  endMeetingForEveryone: vi.fn(),
  joinMeetingAttendance: vi.fn(),
  leaveMeetingAttendance: vi.fn(),
  recordMeetingRecordingConsent: vi.fn(),
  registerParticipantTracks: vi.fn(),
  startMeetingRecording: vi.fn(),
  stopMeetingRecording: vi.fn(),
  uploadMeetingRecording: vi.fn(),
  uploadParticipantTrack: vi.fn(),
}))

vi.mock('../hooks/useMeetingRoom', () => ({
  useMeetingRoom: () => roomValue,
}))

vi.mock('../hooks/useMeetingRecording', () => ({
  useMeetingRecording: () => ({}),
}))

vi.mock('../components/meetings/LocalMediaPreview', () => ({
  LocalMediaPreview: () => <div />,
}))

vi.mock('../components/meetings/OrganizerGuestLobby', () => ({
  OrganizerGuestLobby: () => <div />,
}))

vi.mock('../components/meetings/TranscriptPanel', () => ({
  TranscriptPanel: () => <div />,
}))

const meeting: MeetingDetails = {
  id: 'meeting-current',
  title: 'Quarterly planning',
  agenda: null,
  start_at: '2026-10-05T10:00:00Z',
  end_at: '2026-10-05T11:00:00Z',
  status: 'scheduled',
  organizer_id: 'meeting-owner',
  is_valid: true,
  is_expired: false,
}

const summary: MeetingSummary = {
  id: 'summary-1',
  meeting_id: 'meeting-current',
  transcript_id: 'transcript-1',
  status: 'processing',
  overview: null,
  decisions: null,
  discussion_topics: null,
  open_questions: null,
  created_at: '2026-10-05T11:00:00Z',
  updated_at: '2026-10-05T11:01:00Z',
  completed_at: null,
  error_message: null,
}

function renderMeetingRoom() {
  return render(
    <MemoryRouter initialEntries={['/app/meetings/meeting-current']}>
      <Routes>
        <Route path="/app/meetings/:meetingId" element={<MeetingRoomPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MeetingRoomPage summary', () => {
  it('fetches and displays the persisted summary status for the current meeting', async () => {
    vi.mocked(getMeeting).mockResolvedValue(meeting)
    vi.mocked(getMeetingSummary).mockResolvedValue(summary)

    renderMeetingRoom()

    expect(await screen.findByRole('heading', { name: 'Meeting Summary' })).toBeInTheDocument()
    await waitFor(() => {
      expect(getMeetingSummary).toHaveBeenCalledWith(
        'meeting-current',
        'meeting-owner',
      )
    })
    expect(screen.getByRole('status')).toHaveTextContent(
      'Meeting summary is being processed.',
    )
  })
})
