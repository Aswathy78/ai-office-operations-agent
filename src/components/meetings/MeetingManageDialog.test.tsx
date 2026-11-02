// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MeetingManageDialog } from './MeetingManageDialog'
import {
  addMeetingParticipants,
  cancelMeeting,
  getMeetingAudit,
  getMeetingParticipants,
  removeMeetingParticipant,
} from '../../api/meetings'

vi.mock('../../api/meetings', () => ({
  addMeetingParticipants: vi.fn(),
  cancelMeeting: vi.fn(),
  getMeetingAudit: vi.fn(),
  getMeetingParticipants: vi.fn(),
  removeMeetingParticipant: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MeetingManageDialog audit history', () => {
  it('refreshes the audit history after adding a participant', async () => {
    vi.mocked(getMeetingParticipants).mockResolvedValue([])
    vi.mocked(getMeetingAudit)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([{
        id: 'audit-1',
        meeting_id: 'meeting-1',
        actor_id: 'employee-1',
        action: 'participant_added',
        details: {
          user_id: 'guest-1',
          email: 'guest@example.test',
          notifications: [{ recipient: 'guest@example.test', status: 'sent', error: '' }],
          internal_payload: { secret: 'must not render' },
        },
        created_at: '2026-10-19T00:00:00Z',
      }])
    vi.mocked(addMeetingParticipants).mockResolvedValue({
      message: 'Participant added.',
      added: ['guest@example.test'],
      already_present: [],
      duplicate_addresses: [],
      invalid_addresses: [],
      failed_addresses: [],
      notifications_sent: ['guest@example.test'],
      notification_failures: [],
    })

    render(
      <MeetingManageDialog
        meetingId="meeting-1"
        meetingTitle="Planning"
        meetingStatus="scheduled"
        cancellationPending={false}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    await screen.findByText('No recorded changes.')
    fireEvent.change(screen.getByLabelText('Add by email'), {
      target: { value: 'guest@example.test' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText('1 participant added; 0 addresses were invalid or failed.')).toBeInTheDocument()
    await waitFor(() => expect(getMeetingAudit).toHaveBeenCalledTimes(2))
    expect(screen.getByText('Participant Added')).toBeInTheDocument()
    expect(screen.getByText('Meeting ID: meeting-1')).toBeInTheDocument()
    expect(screen.getByText('User ID: guest-1')).toBeInTheDocument()
    expect(screen.getByText('User Email: guest@example.test')).toBeInTheDocument()
    expect(screen.getByText('Email notification status: Sent')).toBeInTheDocument()
    expect(screen.queryByText(/internal_payload|must not render|employee-1/)).not.toBeInTheDocument()
  })

  it('keeps the dialog open while adding two participants consecutively', async () => {
    const firstParticipant = {
      id: 'participant-1',
      user_id: null,
      name: 'First Guest',
      email: 'first@example.test',
      role: 'participant',
    }
    const secondParticipant = {
      id: 'participant-2',
      user_id: null,
      name: 'Second Guest',
      email: 'second@example.test',
      role: 'participant',
    }
    const onChanged = vi.fn()
    vi.mocked(getMeetingParticipants)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([firstParticipant])
      .mockResolvedValueOnce([firstParticipant, secondParticipant])
    vi.mocked(getMeetingAudit).mockResolvedValue([])
    vi.mocked(addMeetingParticipants)
      .mockResolvedValueOnce({
        message: '1 participant added; 0 addresses were invalid or failed.',
        added: ['first@example.test'],
        already_present: [],
        duplicate_addresses: [],
        invalid_addresses: [],
        failed_addresses: [],
        notifications_sent: ['first@example.test'],
        notification_failures: [],
      })
      .mockResolvedValueOnce({
        message: '1 participant added; 0 addresses were invalid or failed.',
        added: ['second@example.test'],
        already_present: [],
        duplicate_addresses: [],
        invalid_addresses: [],
        failed_addresses: [],
        notifications_sent: ['second@example.test'],
        notification_failures: [],
      })

    render(
      <MeetingManageDialog
        meetingId="meeting-1"
        meetingTitle="Planning"
        meetingStatus="scheduled"
        cancellationPending={false}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    )

    await screen.findByText('No participants are currently invited.')
    const emailInput = screen.getByLabelText('Add by email')
    fireEvent.change(emailInput, { target: { value: 'first@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText('First Guest')).toBeInTheDocument()
    expect(emailInput).toHaveValue('')
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.change(emailInput, { target: { value: 'second@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText('Second Guest')).toBeInTheDocument()
    expect(await screen.findByText('1 participant added; 0 addresses were invalid or failed.')).toBeInTheDocument()
    expect(emailInput).toHaveValue('')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(addMeetingParticipants).toHaveBeenCalledTimes(2)
    expect(getMeetingParticipants).toHaveBeenCalledTimes(3)
    expect(getMeetingAudit).toHaveBeenCalledTimes(3)
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('keeps the dialog open and refreshes participant and audit state after removal', async () => {
    const participant = {
      id: 'participant-1',
      user_id: null,
      name: 'Guest',
      email: 'guest@example.test',
      role: 'participant',
    }
    const onChanged = vi.fn()
    vi.mocked(getMeetingParticipants)
      .mockResolvedValueOnce([participant])
      .mockResolvedValueOnce([])
    vi.mocked(getMeetingAudit)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([{
        id: 'audit-1',
        meeting_id: 'meeting-1',
        actor_id: 'employee-1',
        action: 'participant_removed',
        details: {
          user_id: 'guest-1',
          email: 'guest@example.test',
          notifications: [{ recipient: 'guest@example.test', status: 'failed', error: 'SMTP detail' }],
        },
        created_at: '2026-10-19T00:00:00Z',
      }])
    vi.mocked(removeMeetingParticipant).mockResolvedValue({
      status: 'applied',
      message: 'Participant removed.',
      meeting: null,
      request_id: null,
      notifications_sent: ['guest@example.test'],
      notification_failures: [],
    })

    render(
      <MeetingManageDialog
        meetingId="meeting-1"
        meetingTitle="Planning"
        meetingStatus="scheduled"
        cancellationPending={false}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    )

    expect(await screen.findByText('Guest')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove Guest' }))

    expect(await screen.findByText('No participants are currently invited.')).toBeInTheDocument()
    expect(await screen.findByText('Participant Removed')).toBeInTheDocument()
    expect(screen.getByText('User ID: guest-1')).toBeInTheDocument()
    expect(screen.getByText('User Email: guest@example.test')).toBeInTheDocument()
    expect(screen.getByText('Email notification status: Failed')).toBeInTheDocument()
    expect(screen.queryByText(/SMTP detail|employee-1/)).not.toBeInTheDocument()
    expect(await screen.findByText('Update sent to guest@example.test.')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(removeMeetingParticipant).toHaveBeenCalledWith('meeting-1', 'participant-1')
    expect(getMeetingParticipants).toHaveBeenCalledTimes(2)
    expect(getMeetingAudit).toHaveBeenCalledTimes(2)
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('shows pending and not-sent statuses and handles missing participant details', async () => {
    vi.mocked(getMeetingParticipants).mockResolvedValue([])
    vi.mocked(getMeetingAudit).mockResolvedValue([
      {
        id: 'audit-pending',
        meeting_id: 'meeting-1',
        actor_id: 'actor-1',
        action: 'participant_added',
        details: {
          user_id: 'guest-2',
          email: 'pending@example.test',
          notifications: [{ recipient: 'pending@example.test', status: 'pending' }],
        },
        created_at: '2026-10-19T00:00:00Z',
      },
      {
        id: 'audit-no-email',
        meeting_id: 'meeting-1',
        actor_id: 'actor-1',
        action: 'participant_removed',
        details: {},
        created_at: 'invalid-date',
      },
    ])

    render(
      <MeetingManageDialog
        meetingId="meeting-1"
        meetingTitle="Planning"
        meetingStatus="scheduled"
        cancellationPending={false}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    expect(await screen.findByText('Email notification status: Pending')).toBeInTheDocument()
    expect(screen.getByText('Email notification status: Not Sent')).toBeInTheDocument()
    expect(screen.getAllByText('User ID: Not available')).toHaveLength(1)
    expect(screen.getAllByText('User Email: Not available')).toHaveLength(1)
    expect(screen.getByText('Date and time: Not available')).toBeInTheDocument()
  })

  it('normalizes bulk addresses, skips invalid and duplicate addresses, and reports partial failures', async () => {
    const existingParticipant = {
      id: 'existing-1',
      user_id: null,
      name: 'Existing Guest',
      email: 'existing@example.test',
      role: 'participant',
    }
    const addedOne = {
      id: 'added-1',
      user_id: null,
      name: 'First Guest',
      email: 'first@example.test',
      role: 'participant',
    }
    const addedTwo = {
      id: 'added-2',
      user_id: null,
      name: 'Second Guest',
      email: 'second@example.test',
      role: 'participant',
    }
    vi.mocked(getMeetingParticipants)
      .mockResolvedValueOnce([existingParticipant])
      .mockResolvedValueOnce([existingParticipant, addedOne, addedTwo])
    vi.mocked(getMeetingAudit).mockResolvedValue([])
    vi.mocked(addMeetingParticipants).mockResolvedValue({
      message: '2 participants added; 1 address was invalid or failed.',
      added: ['first@example.test', 'second@example.test'],
      already_present: [],
      duplicate_addresses: [],
      invalid_addresses: [],
      failed_addresses: [],
      notifications_sent: ['first@example.test'],
      notification_failures: ['second@example.test'],
    })

    render(
      <MeetingManageDialog
        meetingId="meeting-1"
        meetingTitle="Planning"
        meetingStatus="scheduled"
        cancellationPending={false}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    await screen.findByText('Existing Guest')
    const emailInput = screen.getByLabelText('Add by email')
    fireEvent.change(emailInput, {
      target: {
        value: ' FIRST@example.test , invalid-address, first@example.test, existing@example.test, Second@example.test ',
      },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText('Second Guest')).toBeInTheDocument()
    expect(addMeetingParticipants).toHaveBeenCalledWith(
      'meeting-1',
      ['first@example.test', 'second@example.test'],
    )
    expect(await screen.findByRole('status')).toHaveTextContent(
      '2 participants added; 2 addresses were invalid or failed.',
    )
    expect(screen.getByRole('status')).toHaveTextContent('Invitation failed: second@example.test')
    expect(screen.getByRole('status')).toHaveTextContent('Invalid: invalid-address')
    expect(screen.getByRole('status')).toHaveTextContent('2 duplicate or already invited addresses skipped.')
    expect(emailInput).toHaveValue('')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
