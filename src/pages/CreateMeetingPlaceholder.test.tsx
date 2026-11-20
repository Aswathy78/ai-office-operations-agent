// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { createMeeting, getCalendarMeetings } from '../api/meetings'
import { CreateMeetingPlaceholder } from './CreateMeetingPlaceholder'

const { authValue } = vi.hoisted(() => ({
  authValue: {
    user: {
      id: 'employee-1',
      name: 'Maya Thompson',
      email: 'maya@example.test',
    },
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/meetings', () => ({
  createMeeting: vi.fn(),
  getCalendarMeetings: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function openInstantMeetingForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /start an instant meeting/i }))
}

describe('CreateMeetingPlaceholder participants', () => {
  it('sends organization-local scheduled times as explicit UTC instants', async () => {
    const user = userEvent.setup()
    vi.mocked(getCalendarMeetings).mockResolvedValue({
      timezone: 'Asia/Kolkata',
      meetings: [],
    })
    vi.mocked(createMeeting).mockResolvedValue({
      id: 'meeting-1',
      title: 'Working-hours meeting',
      join_url: 'http://localhost:5173/meetings/meeting-1',
      status: 'scheduled',
      start_at: '2026-10-19T04:31:00Z',
      end_at: '2026-10-19T06:32:00Z',
      timezone: 'Asia/Kolkata',
      invitations_sent: [],
      invitation_failures: [],
    })
    render(<MemoryRouter><CreateMeetingPlaceholder /></MemoryRouter>)

    await user.click(screen.getByRole('button', { name: /schedule for later/i }))
    await user.type(screen.getByLabelText('Meeting title'), 'Working-hours meeting')
    await user.type(screen.getByLabelText('Start time'), '2026-10-19T10:01')
    await user.type(screen.getByLabelText('End time'), '2026-10-19T12:02')
    await user.click(screen.getByRole('button', { name: 'Schedule meeting' }))

    await waitFor(() => {
      expect(createMeeting).toHaveBeenCalledWith(
        expect.objectContaining({
          start_at: '2026-10-19T04:31:00.000Z',
          end_at: '2026-10-19T06:32:00.000Z',
        }),
        'employee-1',
      )
    })
  })

  it('includes a typed participant even when Create is submitted without clicking Add', async () => {
    const user = userEvent.setup()
    vi.mocked(createMeeting).mockResolvedValue({
      id: 'meeting-1',
      title: 'Story 5.4 test',
      join_url: 'http://localhost:5173/meetings/meeting-1',
      status: 'instant',
      start_at: '2026-10-19T00:00:00Z',
      end_at: '2026-10-19T04:00:00Z',
      timezone: 'UTC',
      invitations_sent: ['rahul@example.test'],
      invitation_failures: [],
    })
    render(<MemoryRouter><CreateMeetingPlaceholder /></MemoryRouter>)

    await openInstantMeetingForm(user)
    await user.type(screen.getByLabelText('Meeting title'), 'Story 5.4 test')
    await user.type(screen.getByLabelText(/invite participants/i), 'rahul@example.test')
    await user.click(screen.getByRole('button', { name: /start an instant meeting/i }))

    expect(createMeeting).toHaveBeenCalledWith(
      expect.objectContaining({ participants: [{ email: 'rahul@example.test' }] }),
      'employee-1',
    )
  })

  it('sends an empty participant list when no participants were entered', async () => {
    const user = userEvent.setup()
    vi.mocked(createMeeting).mockResolvedValue({
      id: 'meeting-1',
      title: 'No attendees',
      join_url: 'http://localhost:5173/meetings/meeting-1',
      status: 'instant',
      start_at: '2026-10-19T00:00:00Z',
      end_at: '2026-10-19T04:00:00Z',
      timezone: 'UTC',
      invitations_sent: [],
      invitation_failures: [],
    })
    render(<MemoryRouter><CreateMeetingPlaceholder /></MemoryRouter>)

    await openInstantMeetingForm(user)
    await user.type(screen.getByLabelText('Meeting title'), 'No attendees')
    await user.click(screen.getByRole('button', { name: /start an instant meeting/i }))

    expect(createMeeting).toHaveBeenCalledWith(
      expect.objectContaining({ participants: [] }),
      'employee-1',
    )
  })

  it('deduplicates added participant emails without regard to case', async () => {
    const user = userEvent.setup()
    vi.mocked(createMeeting).mockResolvedValue({
      id: 'meeting-1',
      title: 'Duplicate attendee',
      join_url: 'http://localhost:5173/meetings/meeting-1',
      status: 'instant',
      start_at: '2026-10-19T00:00:00Z',
      end_at: '2026-10-19T04:00:00Z',
      timezone: 'UTC',
      invitations_sent: ['rahul@example.test'],
      invitation_failures: [],
    })
    render(<MemoryRouter><CreateMeetingPlaceholder /></MemoryRouter>)

    await openInstantMeetingForm(user)
    await user.type(screen.getByLabelText('Meeting title'), 'Duplicate attendee')
    const emailInput = screen.getByLabelText(/invite participants/i)
    await user.type(emailInput, 'rahul@example.test')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await user.type(emailInput, 'RAHUL@example.test')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await user.click(screen.getByRole('button', { name: /start an instant meeting/i }))

    expect(createMeeting).toHaveBeenCalledWith(
      expect.objectContaining({ participants: [{ email: 'rahul@example.test' }] }),
      'employee-1',
    )
  })

  it('rejects an invalid participant email before creating the meeting', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter><CreateMeetingPlaceholder /></MemoryRouter>)

    await openInstantMeetingForm(user)
    await user.type(screen.getByLabelText('Meeting title'), 'Invalid attendee')
    await user.type(screen.getByLabelText(/invite participants/i), 'not-an-email')
    await user.click(screen.getByRole('button', { name: /start an instant meeting/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/valid email/i)
    expect(createMeeting).not.toHaveBeenCalled()
  })

  it('shows invitation delivery failures after successful meeting creation', async () => {
    const user = userEvent.setup()
    vi.mocked(getCalendarMeetings).mockResolvedValue({
      timezone: 'Asia/Kolkata',
      meetings: [],
    })
    vi.mocked(createMeeting).mockResolvedValue({
      id: 'meeting-1',
      title: 'Invitation failure',
      join_url: 'http://localhost:5173/meetings/meeting-1',
      status: 'scheduled',
      start_at: '2026-10-19T00:00:00Z',
      end_at: '2026-10-19T01:00:00Z',
      timezone: 'UTC',
      invitations_sent: [],
      invitation_failures: ['rahul@example.test'],
    })
    render(<MemoryRouter><CreateMeetingPlaceholder /></MemoryRouter>)

    await user.click(screen.getByRole('button', { name: /schedule for later/i }))
    await user.type(screen.getByLabelText('Meeting title'), 'Invitation failure')
    await user.type(screen.getByLabelText(/invite participants/i), 'rahul@example.test')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await user.type(screen.getByLabelText('Start time'), '2026-10-20T10:00')
    await user.type(screen.getByLabelText('End time'), '2026-10-20T11:00')
    await user.click(screen.getByRole('button', { name: 'Schedule meeting' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'invitations could not be delivered to: rahul@example.test',
    )
  })
})
