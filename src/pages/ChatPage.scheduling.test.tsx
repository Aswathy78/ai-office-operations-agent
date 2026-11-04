// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatPage, isMeetingSchedulingIntent } from './ChatPage'
import { confirmMeetingSlot, scheduleMeetingNL } from '../api/meetings'
import { createEmailDraft, getDirectoryGroups, getMyEmailDrafts } from '../api/emailDrafts'
import type { ScheduleResult } from '../types/meeting'

const { authValue } = vi.hoisted(() => ({
  authValue: {
    user: {
      id: 'emp-1',
      name: 'Maya Thompson',
      email: 'maya@northstar.example',
      role: 'employee',
      department: 'Engineering',
    },
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/meetings', () => ({
  confirmMeetingSlot: vi.fn(),
  scheduleMeetingNL: vi.fn(),
}))

vi.mock('../api/emailDrafts', () => ({
  EMAIL_STATUS_POLL_MS: 10000,
  createEmailDraft: vi.fn(),
  getDirectoryGroups: vi.fn().mockResolvedValue([]),
  getMyEmailDrafts: vi.fn().mockResolvedValue([]),
  isInFlight: () => false,
}))

describe('User Story 5.1 — Natural-Language Meeting Scheduling in Chat', () => {
  beforeEach(() => {
    vi.mocked(getDirectoryGroups).mockResolvedValue([
      { name: 'Engineering', count: 5 },
    ])
    vi.mocked(getMyEmailDrafts).mockResolvedValue([])
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  describe('Intent detection', () => {
    it('detects meeting scheduling intent correctly', () => {
      expect(isMeetingSchedulingIntent('Schedule a project meeting tomorrow at 10 AM with the development team')).toBe(true)
      expect(isMeetingSchedulingIntent('Book a sync with Ava tomorrow at 2 PM')).toBe(true)
      expect(isMeetingSchedulingIntent('Set up a meeting with Marcus on Friday')).toBe(true)
      expect(isMeetingSchedulingIntent('Schedule a 45 minute sync tomorrow at 10 AM')).toBe(true)
      expect(isMeetingSchedulingIntent('Schedule demo with the sales team')).toBe(true)
    })

    it('detects meeting rescheduling intent correctly', () => {
      expect(isMeetingSchedulingIntent("Move tomorrow's meeting to 3 PM")).toBe(true)
      expect(isMeetingSchedulingIntent('Reschedule my project meeting to 4 PM')).toBe(true)
      expect(isMeetingSchedulingIntent('Shift the meeting to Friday at 10 AM')).toBe(true)
      expect(isMeetingSchedulingIntent("Push tomorrow's sync to 2 PM")).toBe(true)
      expect(isMeetingSchedulingIntent('Postpone the standup to next Monday')).toBe(true)
      expect(isMeetingSchedulingIntent("Change the time of tomorrow's meeting to 3 PM")).toBe(true)
    })

    it('preserves email drafting intent', () => {
      expect(isMeetingSchedulingIntent("Email the development team a reminder about tomorrow's sprint demo at 10am")).toBe(false)
      expect(isMeetingSchedulingIntent('Send an email to Ava about the deadline')).toBe(false)
      expect(isMeetingSchedulingIntent('Draft an email to finance team')).toBe(false)
      expect(isMeetingSchedulingIntent('Write an email to the board')).toBe(false)
      expect(isMeetingSchedulingIntent('Let the finance team know I will be on leave')).toBe(false)
    })
  })

  describe('Meeting scheduling flow', () => {
    it('successfully schedules a meeting with the development team', async () => {
      const user = userEvent.setup()
      const mockResult: ScheduleResult = {
        status: 'Completed',
        timezone: 'Asia/Kolkata',
        meeting: {
          id: 'meeting-123',
          title: 'Project Meeting',
          start_at: '2026-10-17T04:30:00Z',
          end_at: '2026-10-17T05:00:00Z',
          join_url: 'http://localhost:5173/meetings/meeting-123',
          status: 'scheduled',
          organizer_id: 'emp-1',
          timezone: 'Asia/Kolkata',
          agenda: 'Scheduled via AI Assistant: Schedule a project meeting tomorrow at 10 AM with the development team',
        },
        clarifying_question: null,
        conflicts: [],
        message: 'Scheduled "Project Meeting" for Saturday, October 17 at 10:00 AM (30 minutes) with 5 attendee(s).',
        resolved_attendees: [
          { id: 'emp-1', name: 'Maya Thompson', email: 'maya@northstar.example', team: 'Engineering' },
          { id: 'emp-2', name: 'Ava Patel', email: 'ava.patel@northstar.example', team: 'Engineering' },
          { id: 'emp-3', name: 'Marcus Chen', email: 'marcus.chen@northstar.example', team: 'Engineering' },
          { id: 'emp-4', name: 'Daniel Brooks', email: 'daniel.brooks@northstar.example', team: 'Engineering' },
          { id: 'emp-5', name: 'Rahul', email: 'rahul@northstar.example', team: 'Engineering' },
        ],
        invitations_sent: [
          'maya@northstar.example',
          'ava.patel@northstar.example',
          'marcus.chen@northstar.example',
          'daniel.brooks@northstar.example',
          'rahul@northstar.example',
        ],
        invitation_failures: [],
        reschedule_request_id: null,
      }
      vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(mockResult)

      render(<ChatPage />)

      const textarea = screen.getByLabelText('Message Corporate Chatbox')
      await user.type(textarea, 'Schedule a project meeting tomorrow at 10 AM with the development team')
      await user.click(screen.getByRole('button', { name: 'Send message' }))

      expect(scheduleMeetingNL).toHaveBeenCalledWith(
        'Schedule a project meeting tomorrow at 10 AM with the development team',
        expect.objectContaining({ id: 'emp-1' }),
      )

      await waitFor(() => {
        expect(screen.getByText('Project Meeting')).toBeInTheDocument()
      })

      expect(screen.getByText(/Local Calendar/i)).toBeInTheDocument()
      expect(screen.getByText('Join Meeting Link')).toBeInTheDocument()
      expect(screen.getByText('Ava Patel')).toBeInTheDocument()
      expect(screen.getByText('Marcus Chen')).toBeInTheDocument()
      expect(screen.getByText('Invitations sent to 5 attendee(s)')).toBeInTheDocument()
      expect(createEmailDraft).not.toHaveBeenCalled()
    })

    it('displays clarifying question when attendees are missing and supports follow-up reply', async () => {
      const user = userEvent.setup()
      const clarificationResult: ScheduleResult = {
        status: 'needs_clarification',
        timezone: 'Asia/Kolkata',
        meeting: null,
        clarifying_question: 'Who should be invited to this meeting? You can invite individual employees or a team such as the development team.',
        conflicts: [],
        message: null,
        resolved_attendees: [],
        invitations_sent: [],
        invitation_failures: [],
        reschedule_request_id: null,
      }
      const successResult: ScheduleResult = {
        status: 'Completed',
        timezone: 'Asia/Kolkata',
        meeting: {
          id: 'meeting-456',
          title: 'Project Meeting',
          start_at: '2026-10-17T04:30:00Z',
          end_at: '2026-10-17T05:00:00Z',
          join_url: 'http://localhost:5173/meetings/meeting-456',
          status: 'scheduled',
          organizer_id: 'emp-1',
          timezone: 'Asia/Kolkata',
          agenda: 'Scheduled via AI Assistant',
        },
        clarifying_question: null,
        conflicts: [],
        message: 'Meeting scheduled successfully.',
        resolved_attendees: [
          { id: 'emp-2', name: 'Ava Patel', email: 'ava.patel@northstar.example', team: 'Engineering' },
        ],
        invitations_sent: ['ava.patel@northstar.example'],
        invitation_failures: [],
        reschedule_request_id: null,
      }

      vi.mocked(scheduleMeetingNL)
        .mockResolvedValueOnce(clarificationResult)
        .mockResolvedValueOnce(successResult)

      render(<ChatPage />)

      const textarea = screen.getByLabelText('Message Corporate Chatbox')
      await user.type(textarea, 'Schedule a project meeting tomorrow at 10 AM')
      await user.click(screen.getByRole('button', { name: 'Send message' }))

      await waitFor(() => {
        expect(screen.getByText(/Who should be invited to this meeting\?/i)).toBeInTheDocument()
      })

      // Follow-up answer
      await user.type(textarea, 'with Ava Patel')
      await user.click(screen.getByRole('button', { name: 'Send message' }))

      // Should combine initial request with follow-up
      await waitFor(() => {
        expect(scheduleMeetingNL).toHaveBeenLastCalledWith(
          'Schedule a project meeting tomorrow at 10 AM with Ava Patel',
          expect.objectContaining({ id: 'emp-1' }),
        )
      })

      expect(await screen.findByText('Meeting scheduled successfully.')).toBeInTheDocument()
    })

    it('displays availability conflicts clearly when double-booking is detected', async () => {
      const user = userEvent.setup()
      const conflictResult: ScheduleResult = {
        status: 'conflict',
        timezone: 'Asia/Kolkata',
        meeting: null,
        clarifying_question: null,
        conflicts: [
          {
            attendee_email: 'marcus.chen@northstar.example',
            attendee_name: 'Marcus Chen',
            conflicting_event_title: 'Sprint Planning',
            conflicting_start_at: '2026-10-17T04:30:00Z',
            conflicting_end_at: '2026-10-17T05:30:00Z',
          },
          {
            attendee_email: 'ava.patel@northstar.example',
            attendee_name: 'Ava Patel',
            conflicting_event_title: 'Design Review',
            conflicting_start_at: '2026-10-17T04:30:00Z',
            conflicting_end_at: '2026-10-17T05:00:00Z',
          },
        ],
        alternative_slots: [
          { start_at: '2026-10-17T05:30:00Z', end_at: '2026-10-17T06:00:00Z' },
        ],
        message: "Cannot schedule meeting due to conflicts: Marcus Chen is busy with 'Sprint Planning'. Please choose another time slot.",
        resolved_attendees: [],
        invitations_sent: [],
        invitation_failures: [],
        reschedule_request_id: null,
      }
      vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(conflictResult)

      render(<ChatPage />)

      const textarea = screen.getByLabelText('Message Corporate Chatbox')
      await user.type(textarea, 'Schedule a meeting tomorrow at 10 AM with Marcus Chen')
      await user.click(screen.getByRole('button', { name: 'Send message' }))

      const conflictCard = await screen.findByLabelText('Meeting scheduling conflict')
      expect(conflictCard).toBeInTheDocument()
      expect(within(conflictCard).getByText('Availability Conflict Detected')).toBeInTheDocument()
      expect(within(conflictCard).getByText('Marcus Chen')).toBeInTheDocument()
      expect(within(conflictCard).getByText(/Sprint Planning/)).toBeInTheDocument()
      expect(within(conflictCard).getByText(/marcus\.chen@northstar\.example/)).toBeInTheDocument()
      expect(within(conflictCard).getByText(/ava\.patel@northstar\.example/)).toBeInTheDocument()
      expect(within(conflictCard).getByText('Alternative slots')).toBeInTheDocument()
      expect(within(conflictCard).getByRole('button', { name: 'Choose slot' })).toBeInTheDocument()
      expect(within(conflictCard).getByText('Not Scheduled')).toBeInTheDocument()
    })

    it('sends a user-selected alternative for confirmation and displays the created meeting', async () => {
      const user = userEvent.setup()
      const slot = { start_at: '2026-10-17T05:30:00Z', end_at: '2026-10-17T06:00:00Z' }
      const conflictResult: ScheduleResult = {
        status: 'conflict',
        timezone: 'UTC',
        meeting: null,
        clarifying_question: null,
        conflicts: [{
          attendee_email: 'ava@example.test',
          attendee_name: 'Ava Patel',
          conflicting_event_title: 'Existing meeting',
          conflicting_start_at: '2026-10-17T05:30:00Z',
          conflicting_end_at: '2026-10-17T06:00:00Z',
        }],
        alternative_slots: [slot],
        message: 'Conflict detected',
        resolved_attendees: [],
        invitations_sent: [],
        invitation_failures: [],
        reschedule_request_id: null,
      }
      const completedResult: ScheduleResult = {
        ...conflictResult,
        status: 'Completed',
        meeting: {
          id: 'confirmed-meeting',
          title: 'Project Meeting',
          start_at: slot.start_at,
          end_at: slot.end_at,
          join_url: 'http://localhost:5173/meetings/confirmed-meeting',
          status: 'scheduled',
          organizer_id: 'emp-1',
          timezone: 'UTC',
          agenda: null,
        },
        conflicts: [],
        alternative_slots: [],
        message: 'Meeting scheduled successfully.',
      }
      vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(conflictResult)
      vi.mocked(confirmMeetingSlot).mockResolvedValueOnce(completedResult)

      render(<ChatPage />)
      const prompt = 'Schedule a project meeting tomorrow at 10 AM with Ava Patel'
      await user.type(screen.getByLabelText('Message Corporate Chatbox'), prompt)
      await user.click(screen.getByRole('button', { name: 'Send message' }))
      const conflictCard = await screen.findByLabelText('Meeting scheduling conflict')
      expect(within(conflictCard).getByText('Not Scheduled')).toBeInTheDocument()
      expect(confirmMeetingSlot).not.toHaveBeenCalled()
      await user.click(within(conflictCard).getByRole('button', { name: 'Choose slot' }))

      await waitFor(() => {
        expect(confirmMeetingSlot).toHaveBeenCalledWith(
          prompt,
          slot.start_at,
          expect.objectContaining({ id: 'emp-1' }),
        )
      })
      expect(await screen.findByText('Project Meeting')).toBeInTheDocument()
      expect(screen.getByText('Meeting scheduled successfully.')).toBeInTheDocument()
    })

    it('shows fresh alternatives when the selected slot became unavailable', async () => {
      const user = userEvent.setup()
      const prompt = 'Schedule a project meeting tomorrow at 10 AM with Ava Patel'
      const initialSlot = {
        start_at: '2026-10-17T05:30:00Z',
        end_at: '2026-10-17T06:00:00Z',
      }
      const refreshedSlot = {
        start_at: '2026-10-17T06:00:00Z',
        end_at: '2026-10-17T06:30:00Z',
      }
      const initialConflict: ScheduleResult = {
        status: 'conflict',
        timezone: 'UTC',
        meeting: null,
        clarifying_question: null,
        conflicts: [{
          attendee_email: 'ava@example.test',
          attendee_name: 'Ava Patel',
          conflicting_event_title: 'Existing meeting',
          conflicting_start_at: initialSlot.start_at,
          conflicting_end_at: initialSlot.end_at,
        }],
        alternative_slots: [initialSlot],
        message: 'Conflict detected',
        resolved_attendees: [],
        invitations_sent: [],
        invitation_failures: [],
        reschedule_request_id: null,
      }
      const staleResult: ScheduleResult = {
        ...initialConflict,
        conflicts: [{
          ...initialConflict.conflicts[0],
          conflicting_event_title: 'Newly booked meeting',
        }],
        alternative_slots: [refreshedSlot],
        message: 'That time is no longer available.',
      }
      vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(initialConflict)
      vi.mocked(confirmMeetingSlot).mockResolvedValueOnce(staleResult)

      render(<ChatPage />)
      await user.type(screen.getByLabelText('Message Corporate Chatbox'), prompt)
      await user.click(screen.getByRole('button', { name: 'Send message' }))
      const conflictCard = await screen.findByLabelText('Meeting scheduling conflict')
      await user.click(within(conflictCard).getByRole('button', { name: 'Choose slot' }))

      const refreshedCard = await screen.findAllByLabelText('Meeting scheduling conflict')
      expect(refreshedCard).toHaveLength(2)
      expect(screen.getByText(/Newly booked meeting/)).toBeInTheDocument()
      expect(screen.getAllByRole('button', { name: 'Choose slot' })).toHaveLength(2)
      expect(confirmMeetingSlot).toHaveBeenCalledWith(
        prompt,
        initialSlot.start_at,
        expect.objectContaining({ id: 'emp-1' }),
      )
    })

    it('preserves existing email drafting flow for non-scheduling prompts', async () => {
      const user = userEvent.setup()
      vi.mocked(createEmailDraft).mockResolvedValueOnce({
        id: 'draft-999',
        created_by_id: 'emp-1',
        created_by_name: 'Maya Thompson',
        created_by_email: 'maya@northstar.example',
        created_by_role: 'employee',
        prompt: "Email the development team a reminder about tomorrow's sprint demo at 10am",
        source: 'llm',
        template_key: null,
        generation_note: null,
        subject: 'Sprint demo reminder',
        body: 'Here is the reminder for tomorrow.',
        recipients: [{ name: 'Dev', email: 'dev@northstar.example', internal: true, source: 'directory' }],
        recipient_count: 5,
        recipient_groups: [{ name: 'Engineering', count: 5 }],
        unresolved_recipients: [],
        approval_required: false,
        approval_reasons: [],
        approval_threshold: 0,
        sensitive_findings: [],
        status: 'draft',
        status_changed_at: null,
        approval_decision: null,
        decided_by: null,
        decided_at: null,
        decision_note: null,
        delivery_results: [],
        sent_at: null,
        queued_at: null,
        attempt_count: 0,
        max_attempts: 3,
        next_attempt_at: null,
        last_error: null,
        acknowledged_sensitive: false,
        discarded_at: null,
        bounces: [],
        escalated_at: null,
        notifications: [],
        created_at: '2026-10-16T12:00:00Z',
        updated_at: '2026-10-16T12:00:00Z',
      })

      render(<ChatPage />)

      const textarea = screen.getByLabelText('Message Corporate Chatbox')
      await user.type(textarea, "Email the development team a reminder about tomorrow's sprint demo at 10am")
      await user.click(screen.getByRole('button', { name: 'Send message' }))

      await waitFor(() => {
        expect(createEmailDraft).toHaveBeenCalled()
      })
      expect(scheduleMeetingNL).not.toHaveBeenCalled()
    })
  })
})

describe('User Story 5.3 — Meeting Rescheduling by Chat', () => {
  beforeEach(() => {
    vi.mocked(getDirectoryGroups).mockResolvedValue([{ name: 'Engineering', count: 5 }])
    vi.mocked(getMyEmailDrafts).mockResolvedValue([])
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  const baseResult: ScheduleResult = {
    status: 'Completed',
    timezone: 'Asia/Kolkata',
    meeting: null,
    clarifying_question: null,
    conflicts: [],
    alternative_slots: [],
    message: null,
    resolved_attendees: [],
    invitations_sent: [],
    invitation_failures: [],
    reschedule_request_id: null,
  }

  it('routes a "Move" command to scheduleMeetingNL (not createEmailDraft)', async () => {
    const user = userEvent.setup()
    const pendingResult: ScheduleResult = {
      ...baseResult,
      status: 'pending_approval',
      message: 'Your reschedule request has been submitted and is awaiting organizer approval.',
      reschedule_request_id: 'req-abc123',
    }
    vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(pendingResult)

    render(<ChatPage />)
    const textarea = screen.getByLabelText('Message Corporate Chatbox')
    await user.type(textarea, "Move tomorrow's meeting to 3 PM")
    await user.click(screen.getByRole('button', { name: 'Send message' }))

    expect(scheduleMeetingNL).toHaveBeenCalledWith(
      "Move tomorrow's meeting to 3 PM",
      expect.objectContaining({ id: 'emp-1' }),
    )
    await waitFor(() => {
      expect(screen.getByText(/reschedule request has been submitted/i)).toBeInTheDocument()
    })
  })

  it('displays pending_approval message without a success meeting card', async () => {
    const user = userEvent.setup()
    const pendingResult: ScheduleResult = {
      ...baseResult,
      status: 'pending_approval',
      message: 'Reschedule pending organizer approval. (Request ID: req-xyz)',
      reschedule_request_id: 'req-xyz',
    }
    vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(pendingResult)

    render(<ChatPage />)
    await user.type(screen.getByLabelText('Message Corporate Chatbox'), 'Shift the meeting to Friday at 10 AM')
    await user.click(screen.getByRole('button', { name: 'Send message' }))

    await waitFor(() => {
      expect(screen.getByText(/Reschedule pending organizer approval/i)).toBeInTheDocument()
    })
    // No success card (no Completed status)
    expect(screen.queryByLabelText('Scheduled meeting details')).not.toBeInTheDocument()
    // Specifically no ScheduledMeetingCard
    expect(screen.queryByText('Join Meeting Link')).not.toBeInTheDocument()
  })

  it('shows clarifying question when multiple meetings match the reschedule prompt', async () => {
    const user = userEvent.setup()
    const clarResult: ScheduleResult = {
      ...baseResult,
      status: 'needs_clarification',
      clarifying_question: 'Which meeting did you want to reschedule — "Sprint Planning" at 10 AM or "Design Review" at 2 PM?',
    }
    vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(clarResult)

    render(<ChatPage />)
    await user.type(screen.getByLabelText('Message Corporate Chatbox'), 'Move my meeting tomorrow to 3 PM')
    await user.click(screen.getByRole('button', { name: 'Send message' }))

    await waitFor(() => {
      expect(screen.getByText(/Which meeting did you want to reschedule/i)).toBeInTheDocument()
    })
    expect(scheduleMeetingNL).toHaveBeenCalledTimes(1)
  })

  it('displays conflict information for a reschedule time-slot conflict', async () => {
    const user = userEvent.setup()
    const conflictResult: ScheduleResult = {
      ...baseResult,
      status: 'conflict',
      message: 'The new time conflicts with an existing meeting for some attendees.',
      conflicts: [
        {
          attendee_email: 'ava.patel@northstar.example',
          attendee_name: 'Ava Patel',
          conflicting_event_title: 'Design Review',
          conflicting_start_at: '2026-10-18T08:30:00Z',
          conflicting_end_at: '2026-10-18T09:00:00Z',
        },
      ],
    }
    vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(conflictResult)

    render(<ChatPage />)
    await user.type(screen.getByLabelText('Message Corporate Chatbox'), 'Reschedule sprint planning to 2 PM')
    await user.click(screen.getByRole('button', { name: 'Send message' }))

    const conflictCard = await screen.findByLabelText('Meeting scheduling conflict')
    expect(conflictCard).toBeInTheDocument()
    expect(screen.getByText(/conflicts with an existing meeting/i)).toBeInTheDocument()
  })

  it('shows an actionable message when the meeting service cannot be reached', async () => {
    const user = userEvent.setup()
    vi.mocked(scheduleMeetingNL).mockRejectedValueOnce(new TypeError('Failed to fetch'))

    render(<ChatPage />)
    await user.type(
      screen.getByLabelText('Message Corporate Chatbox'),
      'Schedule a project meeting tomorrow at 9 AM with Ava',
    )
    await user.click(screen.getByRole('button', { name: 'Send message' }))

    await waitFor(() => {
      expect(screen.getByText(/meeting service could not be reached/i)).toBeInTheDocument()
    })
  })

  it('preserves normal scheduling behavior for non-reschedule prompts', async () => {
    const user = userEvent.setup()
    const completedResult: ScheduleResult = {
      ...baseResult,
      status: 'Completed',
      meeting: {
        id: 'meet-1',
        title: 'New Meeting',
        start_at: '2026-10-20T04:30:00Z',
        end_at: '2026-10-20T05:00:00Z',
        join_url: 'http://localhost:5173/meetings/meet-1',
        status: 'scheduled',
        organizer_id: 'emp-1',
        timezone: 'Asia/Kolkata',
        agenda: null,
      },
      message: 'Scheduled "New Meeting" successfully.',
      resolved_attendees: [{ id: 'emp-2', name: 'Ava Patel', email: 'ava@example.com', team: null }],
      invitations_sent: ['ava@example.com'],
    }
    vi.mocked(scheduleMeetingNL).mockResolvedValueOnce(completedResult)

    render(<ChatPage />)
    await user.type(
      screen.getByLabelText('Message Corporate Chatbox'),
      'Schedule a meeting with Ava tomorrow at 10 AM',
    )
    await user.click(screen.getByRole('button', { name: 'Send message' }))

    await waitFor(() => {
      expect(screen.getByText('New Meeting')).toBeInTheDocument()
    })
    expect(scheduleMeetingNL).toHaveBeenCalledTimes(1)
  })
})
