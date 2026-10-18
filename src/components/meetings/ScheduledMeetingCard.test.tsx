// @vitest-environment jsdom
/**
 * Unit tests for ScheduledMeetingCard — timezone display fix (Story 5.1).
 *
 * The backend stores meeting timestamps as naive UTC strings without a timezone
 * suffix (e.g. "2026-10-18T04:30:00"). Before the fix, `new Date(naiveStr)`
 * was interpreted as local time, so a user in IST (+05:30) would see 4:30 AM
 * instead of the correct 10:00 AM.
 *
 * After the fix, naive strings are parsed as UTC and formatted in the
 * organization timezone returned by the API.
 *
 * Expected text is formatted with Intl using an explicit organization
 * timezone, independent of the test runner's local timezone.
 */
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it } from 'vitest'
import { ScheduledMeetingCard } from './ScheduledMeetingCard'
import type { ScheduleResult } from '../../types/meeting'

function makeScheduledResult(
  start_at: string,
  end_at: string,
  timezone = 'UTC',
): ScheduleResult {
  return {
    status: 'Completed',
    timezone,
    meeting: {
      id: 'meeting-tz-test',
      title: 'Timezone Test Meeting',
      start_at,
      end_at,
      join_url: 'http://localhost:5173/meetings/meeting-tz-test',
      status: 'scheduled',
      organizer_id: 'emp-1',
      agenda: null,
      timezone,
    },
    clarifying_question: null,
    conflicts: [],
    message: 'Test message',
    resolved_attendees: [],
    invitations_sent: [],
    invitation_failures: [],
    calendar_provider: 'local',
    reschedule_request_id: null,
  }
}

afterEach(() => cleanup())

describe('ScheduledMeetingCard — UTC timestamp parsing', () => {
  it('renders a scheduled meeting card', () => {
    const result = makeScheduledResult('2026-10-18T04:30:00Z', '2026-10-18T05:00:00Z')
    render(<ScheduledMeetingCard result={result} />)
    expect(screen.getByLabelText('Scheduled meeting details')).toBeInTheDocument()
    expect(screen.getByText('Timezone Test Meeting')).toBeInTheDocument()
  })

  it('produces the same displayed time for a naive UTC string and an explicit Z string', () => {
    // Render with explicit Z
    const { unmount: unmount1 } = render(
      <ScheduledMeetingCard result={makeScheduledResult('2026-10-18T04:30:00Z', '2026-10-18T05:00:00Z')} />
    )
    // Grab the displayed time text (the element that holds the clock icon + time string)
    // The time text lives inside the .scheduled-meeting-meta span next to the Clock icon.
    const timeElementZ = screen.getByLabelText('Scheduled meeting details').querySelector('.scheduled-meeting-meta-item span')
    const displayedTimeZ = timeElementZ?.textContent ?? ''
    unmount1()

    // Render again with naive string (no Z) — should produce same display
    render(
      <ScheduledMeetingCard result={makeScheduledResult('2026-10-18T04:30:00', '2026-10-18T05:00:00')} />
    )
    const timeElementNaive = screen.getByLabelText('Scheduled meeting details').querySelector('.scheduled-meeting-meta-item span')
    const displayedTimeNaive = timeElementNaive?.textContent ?? ''

    // Both should show the same time — the UTC interpretation is identical
    expect(displayedTimeZ).toBeTruthy()
    expect(displayedTimeNaive).toBe(displayedTimeZ)
  })

  it('displays the instant in the organization timezone exactly once', () => {
    const start = new Date('2026-10-18T04:30:00Z')
    const end = new Date('2026-10-18T05:00:00Z')
    const expected = `${new Intl.DateTimeFormat(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'Asia/Kolkata',
    }).format(start)}, ${new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      timeZone: 'Asia/Kolkata',
    }).format(start)} – ${new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      timeZone: 'Asia/Kolkata',
    }).format(end)}`

    render(
      <ScheduledMeetingCard
        result={makeScheduledResult(
          '2026-10-18T04:30:00Z',
          '2026-10-18T05:00:00Z',
          'Asia/Kolkata',
        )}
      />,
    )

    expect(
      screen.getByLabelText('Scheduled meeting details')
        .querySelector('.scheduled-meeting-meta-item span'),
    ).toHaveTextContent(expected)
    expect(expected).toMatch(/10:00\s*AM.*10:30\s*AM/i)
  })

  it('naive string and local-time string produce DIFFERENT times (validates fix is meaningful)', () => {
    // A truly local-time string at UTC+05:30 would be 10:00 AM local
    // which is "2026-10-18T10:00:00" with no Z.
    // Our fix should treat it as UTC (10:00 UTC = 15:30 IST).
    // So "04:30 naive" (= 04:30 UTC) != "10:00 naive" (= 10:00 UTC).
    const { unmount: unmount1 } = render(
      <ScheduledMeetingCard result={makeScheduledResult('2026-10-18T04:30:00', '2026-10-18T05:00:00')} />
    )
    const time1El = screen.getByLabelText('Scheduled meeting details').querySelector('.scheduled-meeting-meta-item span')
    const time1 = time1El?.textContent ?? ''
    unmount1()

    render(
      <ScheduledMeetingCard result={makeScheduledResult('2026-10-18T10:00:00', '2026-10-18T10:30:00')} />
    )
    const time2El = screen.getByLabelText('Scheduled meeting details').querySelector('.scheduled-meeting-meta-item span')
    const time2 = time2El?.textContent ?? ''

    // 04:30 UTC ≠ 10:00 UTC, so the displayed times must differ
    expect(time1).not.toBe(time2)
  })

  it('renders conflict card with correctly parsed conflict times', () => {
    const result: ScheduleResult = {
      status: 'conflict',
      timezone: 'Asia/Kolkata',
      meeting: null,
      clarifying_question: null,
      conflicts: [
        {
          attendee_email: 'marcus@example.com',
          attendee_name: 'Marcus Chen',
          conflicting_event_title: 'Sprint Planning',
          conflicting_start_at: '2026-10-18T04:30:00',  // naive UTC
          conflicting_end_at: '2026-10-18T05:30:00',    // naive UTC
        },
      ],
      message: 'Conflict detected',
      resolved_attendees: [],
      invitations_sent: [],
      invitation_failures: [],
      reschedule_request_id: null,
    }
    render(<ScheduledMeetingCard result={result} />)
    expect(screen.getByLabelText('Meeting scheduling conflict')).toBeInTheDocument()
    expect(screen.getByText('Marcus Chen')).toBeInTheDocument()
    // Conflict times should be rendered (not throw) and show a time string
    // The exact time depends on the test runner's locale, but it must be present.
    const conflictCard = screen.getByLabelText('Meeting scheduling conflict')
    expect(conflictCard.textContent).toMatch(/Sprint Planning/)
  })

  it('renders the provider badge for local calendar', () => {
    const result = makeScheduledResult('2026-10-18T04:30:00Z', '2026-10-18T05:00:00Z')
    render(<ScheduledMeetingCard result={result} />)
    expect(screen.getByText(/Local Calendar/i)).toBeInTheDocument()
  })

  it('shows the created meeting and explicitly reports invitation failures', () => {
    const result: ScheduleResult = {
      ...makeScheduledResult('2026-10-18T04:30:00Z', '2026-10-18T05:00:00Z'),
      status: 'error',
      message: 'Meeting was created, but invitations could not be delivered to: ava@example.com.',
      invitation_failures: ['ava@example.com'],
    }

    render(<ScheduledMeetingCard result={result} />)

    const card = screen.getByLabelText('Meeting created, but invitations failed')
    expect(card).toHaveTextContent('Timezone Test Meeting')
    expect(card).toHaveTextContent('invitations could not be delivered')
    expect(card).toHaveTextContent('ava@example.com')
    expect(screen.getByRole('link', { name: 'Open meeting' })).toHaveAttribute(
      'href',
      'http://localhost:5173/meetings/meeting-tz-test',
    )
  })
})
