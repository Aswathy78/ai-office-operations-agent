// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { getMeetings } from '../api/meetings'
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
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('MeetingsPage organization timezone display', () => {
  it('formats retrieved UTC timestamps in the organization timezone', async () => {
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
})
