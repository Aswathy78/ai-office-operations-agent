// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import {
  addMeetingParticipants,
  cancelMeeting,
  decideMeetingCancellation,
  getMeetingParticipants,
  getMeetings,
  getPendingMeetingCancellations,
  removeMeetingParticipant,
} from '../api/meetings'
import { MeetingsPage } from './MeetingsPage'

const { authValue } = vi.hoisted(() => ({
  authValue: {
    user: {
      id: 'employee-1',
      name: 'Maya Patel',
      email: 'maya@example.test',
    },
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/meetings', () => ({
  getMeetings: vi.fn(),
  deleteMeeting: vi.fn(),
  getPendingMeetingCancellations: vi.fn(),
  decideMeetingCancellation: vi.fn(),
  getMeetingParticipants: vi.fn(),
  getMeetingAudit: vi.fn(() => Promise.resolve([])),
  addMeetingParticipants: vi.fn(),
  removeMeetingParticipant: vi.fn(),
  cancelMeeting: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('MeetingsPage organization timezone display', () => {
  it('formats retrieved UTC timestamps in the organization timezone', async () => {
    vi.mocked(getPendingMeetingCancellations).mockResolvedValue([])
    vi.mocked(getMeetings).mockResolvedValue([
      {
        id: 'meeting-1',
        title: 'Project Meeting',
        agenda: null,
        start_at: '2026-10-18T04:30:00+00:00',
        end_at: '2026-10-18T05:00:00+00:00',
        join_url: 'http://localhost:5173/meetings/meeting-1',
        status: 'scheduled',
        organizer_id: 'employee-1',
        timezone: 'Asia/Kolkata',
      },
    ])
    const start = new Date('2026-10-18T04:30:00Z')
    const end = new Date('2026-10-18T05:00:00Z')
    const formatInOrganizationTimeZone = (date: Date) =>
      new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone: 'Asia/Kolkata',
      }).format(date)

    render(
      <MemoryRouter>
        <MeetingsPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText(
      `${formatInOrganizationTimeZone(start)} – ${formatInOrganizationTimeZone(end)}`,
    )).toBeInTheDocument()
  })

  it('shows pending cancellation requests and lets an authorized user approve them', async () => {
    vi.mocked(getMeetings).mockResolvedValue([])
    vi.mocked(getPendingMeetingCancellations).mockResolvedValue([{
      id: 'request-1',
      meeting_id: 'meeting-1',
      meeting_title: 'Quarterly planning',
      requested_by: 'employee-2',
      reason: 'Schedule changed',
      status: 'pending',
      decided_by: null,
      decided_at: null,
      decision_note: null,
      created_at: '2030-01-01T10:00:00Z',
      attendee_count: 21,
    }])
    vi.mocked(decideMeetingCancellation).mockResolvedValue({
      request: {
        id: 'request-1', meeting_id: 'meeting-1', meeting_title: 'Quarterly planning',
        requested_by: 'employee-2', reason: 'Schedule changed', status: 'approved',
        decided_by: 'employee-1', decided_at: '2030-01-01T10:10:00Z', decision_note: null,
        created_at: '2030-01-01T10:00:00Z', attendee_count: 21,
      },
      meeting: null,
      message: 'Cancellation approved and meeting cancelled.',
      notifications_sent: [],
      notification_failures: [],
    })

    render(<MemoryRouter><MeetingsPage /></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: 'Cancellation approvals' })).toBeInTheDocument()
    expect(screen.getByText('21 attendees · Schedule changed')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Approve cancellation' }))
    expect(decideMeetingCancellation).toHaveBeenCalledWith('request-1', 'approve')
    expect(await screen.findByRole('status')).toHaveTextContent('Cancellation approved and meeting cancelled.')
  })

  it('shows pending cancellation requests and lets an authorized user reject them', async () => {
    vi.mocked(getMeetings).mockResolvedValue([])
    vi.mocked(getPendingMeetingCancellations)
      .mockResolvedValueOnce([{
      id: 'request-reject',
      meeting_id: 'meeting-reject',
      meeting_title: 'Planning review',
      requested_by: 'employee-2',
      reason: 'Confirm before cancelling',
      status: 'pending',
      decided_by: null,
      decided_at: null,
      decision_note: null,
      created_at: '2030-01-01T10:00:00Z',
      attendee_count: 22,
      }])
      .mockResolvedValue([])
    vi.mocked(decideMeetingCancellation).mockResolvedValue({
      request: {
        id: 'request-reject',
        meeting_id: 'meeting-reject',
        meeting_title: 'Planning review',
        requested_by: 'employee-2',
        reason: 'Confirm before cancelling',
        status: 'rejected',
        decided_by: 'employee-1',
        decided_at: '2030-01-01T10:10:00Z',
        decision_note: null,
        created_at: '2030-01-01T10:00:00Z',
        attendee_count: 22,
      },
      meeting: null,
      message: 'Cancellation request rejected.',
      notifications_sent: [],
      notification_failures: [],
    })

    render(<MemoryRouter><MeetingsPage /></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: 'Cancellation approvals' })).toBeInTheDocument()
    expect(screen.getByText('22 attendees · Confirm before cancelling')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(decideMeetingCancellation).toHaveBeenCalledWith('request-reject', 'reject')
    expect(await screen.findByRole('status')).toHaveTextContent('Cancellation request rejected.')
    expect(screen.queryByRole('heading', { name: 'Planning review' })).not.toBeInTheDocument()
  })

  it('shows a useful error and keeps the request visible when rejection fails', async () => {
    vi.mocked(getMeetings).mockResolvedValue([])
    vi.mocked(getPendingMeetingCancellations).mockResolvedValue([{
      id: 'request-reject-failed',
      meeting_id: 'meeting-reject-failed',
      meeting_title: 'Review awaiting decision',
      requested_by: 'employee-2',
      reason: null,
      status: 'pending',
      decided_by: null,
      decided_at: null,
      decision_note: null,
      created_at: '2030-01-01T10:00:00Z',
      attendee_count: 24,
    }])
    vi.mocked(decideMeetingCancellation).mockRejectedValue(
      new Error('Only the meeting organizer or an authorized delegate can manage this meeting.'),
    )

    render(<MemoryRouter><MeetingsPage /></MemoryRouter>)

    await screen.findByRole('heading', { name: 'Review awaiting decision' })
    await userEvent.click(screen.getByRole('button', { name: 'Reject' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Only the meeting organizer or an authorized delegate can manage this meeting.',
    )
    expect(screen.getByRole('heading', { name: 'Review awaiting decision' })).toBeInTheDocument()
  })

  it('shows cancelled meetings distinctly and provides organizer participant controls', async () => {
    vi.mocked(getPendingMeetingCancellations).mockResolvedValue([])
    vi.mocked(getMeetings).mockResolvedValue([
      {
        id: 'cancelled-1', title: 'Cancelled review', agenda: null,
        start_at: '2030-10-18T04:30:00Z', end_at: '2030-10-18T05:00:00Z',
        join_url: 'http://localhost/meetings/cancelled-1', status: 'cancelled',
        organizer_id: 'employee-1', timezone: 'UTC',
      },
      {
        id: 'scheduled-1', title: 'Planning meeting', agenda: null,
        start_at: '2030-10-19T04:30:00Z', end_at: '2030-10-19T05:00:00Z',
        join_url: 'http://localhost/meetings/scheduled-1', status: 'scheduled',
        organizer_id: 'employee-1', timezone: 'UTC',
      },
    ])
    vi.mocked(getMeetingParticipants).mockResolvedValue([
      { id: 'participant-1', user_id: 'employee-2', name: 'Ava Patel', email: 'ava@example.test', role: 'participant' },
    ])
    vi.mocked(addMeetingParticipants).mockResolvedValue({
      message: '1 participant added; 0 addresses were invalid or failed.',
      added: ['new@example.test'],
      already_present: [],
      duplicate_addresses: [],
      invalid_addresses: [],
      failed_addresses: [],
      notifications_sent: ['new@example.test'], notification_failures: [],
    })
    vi.mocked(removeMeetingParticipant).mockResolvedValue({
      status: 'applied', message: 'Participant removed.', meeting: null, request_id: null,
      notifications_sent: ['ava@example.test'], notification_failures: [],
    })
    vi.mocked(cancelMeeting).mockResolvedValue({
      status: 'applied', message: 'Meeting cancelled.', meeting: null, request_id: null,
      notifications_sent: ['ava@example.test'], notification_failures: [],
    })

    render(<MemoryRouter><MeetingsPage /></MemoryRouter>)

    expect(await screen.findAllByText('Cancelled')).toHaveLength(2)
    await userEvent.click(await screen.findByRole('button', { name: 'Manage Planning meeting' }))
    expect(await screen.findByText('Ava Patel')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Add by email'), 'new@example.test')
    await userEvent.click(screen.getByRole('button', { name: 'Invite' }))
    expect(addMeetingParticipants).toHaveBeenCalledWith('scheduled-1', ['new@example.test'])
    expect(await screen.findByRole('status')).toHaveTextContent('1 participant added; 0 addresses were invalid or failed.')

    await userEvent.click(screen.getByRole('button', { name: 'Remove Ava Patel' }))
    expect(removeMeetingParticipant).toHaveBeenCalledWith('scheduled-1', 'participant-1')
    expect(await screen.findByRole('status')).toHaveTextContent('Update sent to ava@example.test.')

    await userEvent.click(screen.getByRole('button', { name: 'Cancel meeting' }))
    expect(cancelMeeting).toHaveBeenCalledWith('scheduled-1')
    expect(await screen.findByRole('status')).toHaveTextContent('Meeting cancelled.')
  })
})
