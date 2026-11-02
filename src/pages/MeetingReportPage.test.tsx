// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import {
  confirmActionItemOwner,
  extractMeetingActionItems,
  getActiveEmployees,
  getMeetingActionItems,
  getMeetingReport,
  getMeetingSummary,
  getMeetingTranscript,
  MeetingApiError,
  updateMeetingTranscript,
  getMeetingMinutes,
  generateMeetingMinutes,
  approveMeetingMinutes,
  sendMeetingMinutes,
  getMeetingMinutesRecipients,
} from '../api/meetings'
import { MeetingReportPage } from './MeetingReportPage'
import type {
  MeetingActionItems,
  MeetingReport,
  MeetingSummary,
  MeetingTranscript,
  MeetingMinutes,
  MeetingMinutesDeliveryResponse,
} from '../types/meeting'

const { authValue } = vi.hoisted(() => ({
  authValue: { user: { id: 'organizer-1', email: undefined as string | undefined } },
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
  getMeetingReport: vi.fn(),
  getMeetingActionItems: vi.fn(),
  extractMeetingActionItems: vi.fn(),
  getActiveEmployees: vi.fn(),
  confirmActionItemOwner: vi.fn(),
  getMeetingSummary: vi.fn(),
  getMeetingTranscript: vi.fn(),
  getMeetingTranscriptCorrections: vi.fn(),
  updateMeetingTranscript: vi.fn(),
  getMeetingMinutes: vi.fn(),
  generateMeetingMinutes: vi.fn(),
  approveMeetingMinutes: vi.fn(),
  sendMeetingMinutes: vi.fn(),
  getMeetingMinutesRecipients: vi.fn(),
}))
vi.mock('../api/workflows', () => ({
  getMeetingWorkflow: vi.fn().mockResolvedValue({ workflow: null }),
  streamMeetingWorkflow: vi.fn().mockResolvedValue(undefined),
}))

const report: MeetingReport = {
  meeting_id: 'meeting-current',
  title: 'Quarterly planning',
  agenda: null,
  start_at: '2026-10-05T10:00:00Z',
  end_at: '2026-10-05T11:00:00Z',
  organizer_id: 'organizer-1',
  attendance: [
    {
      participant_name: 'Maya',
      participant_email: 'maya@example.com',
      user_id: 'user-1',
      is_external_guest: false,
      joined_at: '2026-10-05T10:00:00Z',
      left_at: '2026-10-05T11:00:00Z',
    },
  ],
}

const transcript: MeetingTranscript = {
  id: 'transcript-1',
  meeting_id: 'meeting-current',
  recording_id: 'recording-1',
  status: 'completed',
  language: 'en',
  transcript_text: 'Thank you very much.',
  segments: [
    {
      start: 0,
      end: 3,
      text: 'Thank you very much.',
      speaker: 'Maya',
      flagged_for_correction: false,
    },
  ],
  error_message: null,
  created_at: '2026-10-05T10:00:00Z',
  processing_started_at: null,
  completed_at: '2026-10-05T10:01:00Z',
}

const summary: MeetingSummary = {
  id: 'summary-1',
  meeting_id: 'meeting-current',
  transcript_id: 'transcript-1',
  status: 'completed',
  overview: 'The team planned the next quarter.',
  decisions: ['Start the pilot in May.'],
  discussion_topics: ['Hiring plan'],
  open_questions: ['Who owns onboarding?'],
  created_at: '2026-10-05T11:00:00Z',
  updated_at: '2026-10-05T11:01:00Z',
  completed_at: '2026-10-05T11:01:00Z',
  error_message: null,
}

const actionItems: MeetingActionItems = {
  id: 'extraction-1',
  meeting_id: 'meeting-current',
  transcript_id: 'transcript-1',
  status: 'completed',
  created_at: '2026-10-05T11:00:00Z',
  updated_at: '2026-10-05T11:01:00Z',
  processing_started_at: '2026-10-05T11:00:30Z',
  completed_at: '2026-10-05T11:01:00Z',
  error_message: null,
  action_items: [
    {
      id: 'item-1',
      owner_name: 'Maya',
      task_description: 'Send the agenda.',
      deadline_at: null,
      deadline_text: 'by Friday',
      priority: 'high',
      confidence: 0.92,
      meeting_id: 'meeting-current',
      transcript_id: 'transcript-1',
      source_segment_index: 0,
      source_start_seconds: 0,
      source_end_seconds: 3,
      source_quote_start_seconds: 0.92,
      source_quote_end_seconds: 2.7,
      source_quote: 'Thank you very much.',
    },
  ],
}

const minutes: MeetingMinutes = {
  id: 'minutes-1',
  meeting_id: 'meeting-current',
  summary_id: 'summary-1',
  status: 'pending_approval',
  policy: 'manual_approval',
  approved_by: null,
  approved_at: null,
  content_summary: 'The team planned the next quarter.',
  content_decisions: ['Start the pilot in May.'],
  content_action_items: [
    {
      id: 'item-1',
      task_description: 'Send the agenda.',
      owner_name: 'Maya',
      owner_email: 'maya@example.com',
      deadline_at: '2026-10-16T17:00:00Z',
      deadline_text: 'by Friday',
      priority: 'high',
    },
  ],
  error_message: null,
  created_at: '2026-10-05T11:05:00Z',
  updated_at: '2026-10-05T11:05:00Z',
  recipients: [
    {
      id: 'recipient-1',
      minutes_id: 'minutes-1',
      recipient_name: 'Maya',
      recipient_email: 'maya@example.com',
      attendance_log_id: 'log-1',
      delivery_status: 'pending',
      error_message: null,
      idempotency_key: 'minutes:minutes-1:maya@example.com',
      sent_at: null,
      created_at: '2026-10-05T11:05:00Z',
    },
  ],
}

function renderReport(
  initialEntry = '/meetings/meeting-current/report',
) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/meetings/:meetingId/report"
          element={<><MeetingReportPage /><CurrentLocation /></>}
        />
      </Routes>
    </MemoryRouter>,
  )
}

function CurrentLocation() {
  const location = useLocation()
  return <div data-testid="current-location">{location.pathname}{location.search}</div>
}

beforeEach(() => {
  authValue.user = { id: 'organizer-1', email: undefined }
  vi.mocked(getMeetingReport).mockResolvedValue(report)
  vi.mocked(getMeetingActionItems).mockResolvedValue(actionItems)
  vi.mocked(extractMeetingActionItems).mockResolvedValue(actionItems)
  vi.mocked(getActiveEmployees).mockResolvedValue([
    { id: 'employee-1', name: 'Ava Patel', email: 'ava.patel@northstar.example' },
  ])
  vi.mocked(confirmActionItemOwner).mockResolvedValue({
    ...actionItems.action_items[0],
    proposed_employee_id: 'employee-1',
    confirmed_employee_id: 'employee-1',
    employee_match_status: 'unique_match',
    ownership_status: 'confirmed',
  })
  vi.mocked(getMeetingSummary).mockResolvedValue(summary)
  vi.mocked(getMeetingTranscript).mockResolvedValue(transcript)
  vi.mocked(updateMeetingTranscript).mockResolvedValue(transcript)
  vi.mocked(getMeetingMinutes).mockResolvedValue(minutes)
  vi.mocked(generateMeetingMinutes).mockResolvedValue(minutes)
  vi.mocked(approveMeetingMinutes).mockResolvedValue({
    ...minutes,
    status: 'approved',
    approved_by: 'organizer-1',
    approved_at: '2026-10-05T11:10:00Z',
  })
  vi.mocked(sendMeetingMinutes).mockResolvedValue({
    minutes_id: 'minutes-1',
    meeting_id: 'meeting-current',
    total: 1,
    sent: 1,
    failed: 0,
    pending: 0,
    recipients: [
      {
        ...minutes.recipients[0],
        delivery_status: 'sent',
        sent_at: '2026-10-05T11:15:00Z',
      },
    ],
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MeetingReportPage transcript', () => {
  it('loads the exact source transcript as the assigned employee', async () => {
    authValue.user = {
      id: 'employee-user',
      email: 'maya.employee@example.test',
    }
    vi.mocked(getMeetingReport).mockResolvedValue({ ...report, attendance: [] })
    renderReport(
      '/meetings/meeting-current/report?transcript_id=transcript-1&segment=0',
    )

    expect(await screen.findByText('Thank you very much.')).toBeInTheDocument()
    expect(getMeetingReport).toHaveBeenCalledWith(
      'meeting-current',
      'employee-user',
      'maya.employee@example.test',
    )
    expect(getMeetingTranscript).toHaveBeenCalledWith(
      'meeting-current',
      'employee-user',
      'transcript-1',
      'maya.employee@example.test',
    )
    await waitFor(() =>
      expect(document.getElementById('meeting-transcript-segment-0'))
      .toHaveClass('is-source-highlighted')
    )
    expect(getMeetingActionItems).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'View Summary' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'View Minutes' })).not.toBeInTheDocument()
  })

  it('keeps meeting report access organizer-only for an unrelated employee', async () => {
    authValue.user = {
      id: 'unrelated-user',
      email: 'unrelated@example.test',
    }
    vi.mocked(getMeetingReport).mockRejectedValue(new Error('Forbidden'))
    renderReport('/meetings/meeting-current/report')

    expect(await screen.findByRole('alert')).toHaveTextContent('Forbidden')
    expect(getMeetingReport).toHaveBeenCalledWith(
      'meeting-current',
      'unrelated-user',
      'unrelated@example.test',
    )
    expect(getMeetingTranscript).not.toHaveBeenCalled()
  })

  it('shows the transcript action while preserving report content', async () => {
    renderReport()

    expect(await screen.findByText('Quarterly planning')).toBeInTheDocument()
    expect(screen.getByText('Attendance Log')).toBeInTheDocument()
    expect(within(screen.getByRole('table')).getByText('Maya')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View Transcript' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View Summary' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View Minutes' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Meeting Summary' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Meeting Minutes' })).not.toBeInTheDocument()
    expect(getMeetingSummary).not.toHaveBeenCalled()
    expect(getMeetingMinutes).not.toHaveBeenCalled()
    expect(getMeetingReport).toHaveBeenCalledWith('meeting-current', 'organizer-1')
  })

  describe('MeetingReportPage summary', () => {
    it('fetches and displays the persisted summary when requested', async () => {
      const user = userEvent.setup()
      renderReport()

      await user.click(await screen.findByRole('button', { name: 'View Summary' }))

      expect(await screen.findByRole('heading', { name: 'Meeting Summary' })).toBeInTheDocument()
      expect(getMeetingSummary).toHaveBeenCalledWith('meeting-current', 'organizer-1')
      expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument()
      expect(screen.getByText('The team planned the next quarter.')).toBeInTheDocument()
      expect(screen.getByText('Start the pilot in May.')).toBeInTheDocument()
      expect(screen.getByText('Hiring plan')).toBeInTheDocument()
      expect(screen.getByText('Who owns onboarding?')).toBeInTheDocument()
      expect(screen.getByText('Attendance Log')).toBeInTheDocument()
    })

    it.each([
      ['pending', 'Meeting summary is pending.'],
      ['processing', 'Meeting summary is being processed.'],
      ['failed', 'Meeting summary generation failed.'],
    ])('displays persisted %s summary status', async (status, message) => {
      const user = userEvent.setup()
      vi.mocked(getMeetingSummary).mockResolvedValue({
        ...summary,
        status: status as MeetingSummary['status'],
        error_message: status === 'failed' ? '<script>summary error</script>' : null,
      })

      renderReport()
      await user.click(await screen.findByRole('button', { name: 'View Summary' }))

      expect(await screen.findByText(message)).toBeInTheDocument()
      if (status === 'failed') {
        expect(screen.getByText('<script>summary error</script>').tagName).toBe('P')
        expect(document.querySelector('script')).not.toBeInTheDocument()
      }
    })

    it('displays an unavailable state when no persisted summary exists', async () => {
      const user = userEvent.setup()
      vi.mocked(getMeetingSummary).mockRejectedValue(
        new MeetingApiError('Meeting summary not found', 404),
      )

      renderReport()
      await user.click(await screen.findByRole('button', { name: 'View Summary' }))

      expect(await screen.findByText('Meeting summary not available.')).toBeInTheDocument()
    })

    it('hides the summary without affecting transcript visibility', async () => {
      const user = userEvent.setup()
      renderReport()
      await user.click(await screen.findByRole('button', { name: 'View Summary' }))
      expect(await screen.findByRole('heading', { name: 'Meeting Summary' })).toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Hide Summary' }))
      expect(screen.queryByRole('heading', { name: 'Meeting Summary' })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'View Transcript' })).toBeInTheDocument()
    })
  })

  it('requests the current meeting transcript and renders its segments', async () => {
    const user = userEvent.setup()
    renderReport()
    await screen.findByText('Quarterly planning')

    await user.click(screen.getByRole('button', { name: 'View Transcript' }))

    expect(await screen.findByRole('heading', { name: 'Meeting Transcript' })).toBeInTheDocument()
    expect(getMeetingTranscript).toHaveBeenCalledWith('meeting-current', 'organizer-1')
    expect(
      within(screen.getByLabelText('Meeting transcript')).getByText('Thank you very much.'),
    ).toBeInTheDocument()
    expect(within(screen.getByLabelText('Meeting transcript')).getByText('Maya')).toBeInTheDocument()
    expect(screen.getByText('0:00–0:03')).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: 'Search transcript' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit segment 1' })).toBeInTheDocument()
  })

  describe('MeetingReportPage action items', () => {
    it('refreshes the employee directory when the loaded list is empty', async () => {
      const user = userEvent.setup()
      vi.mocked(getActiveEmployees)
        .mockResolvedValueOnce([])
        .mockResolvedValueOnce([
          { id: 'rahul-1', name: 'Rahul', email: 'rahul@example.test' },
        ])
      renderReport()

      expect(await screen.findByText(
        'No active employees are available to assign.',
      )).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Refresh employee list' }))

      expect(await screen.findByRole('option', {
        name: 'Rahul (rahul@example.test)',
      })).toBeInTheDocument()
      expect(getActiveEmployees).toHaveBeenCalledTimes(2)
    })

    it('renders extracted action items and their source reference', async () => {
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          {
            ...actionItems.action_items[0],
            proposed_employee_id: 'employee-1',
            employee_match_status: 'unique_match',
            ownership_status: 'proposed',
          },
        ],
      })
      renderReport()

      expect(await screen.findByRole('heading', { name: 'Meeting Action Items' })).toBeInTheDocument()
      expect(getMeetingActionItems).toHaveBeenCalledWith('meeting-current', 'organizer-1')
      const panel = screen.getByRole('heading', { name: 'Meeting Action Items' }).closest('section')
      expect(panel).not.toBeNull()
      expect(await within(panel as HTMLElement).findByText('Send the agenda.')).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText('Maya')).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText('Unique match')).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText('Proposed')).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText('by Friday')).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText('high')).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText('92%')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Source transcript phrase: 0:00.92' })).toBeInTheDocument()
      expect(within(panel as HTMLElement).getByText(
        'Extraction status: completed. 1 action item extracted.',
      )).toBeInTheDocument()
      expect(screen.queryByText('No action items were identified.')).not.toBeInTheDocument()
    })

    it('confirms a selected active employee and refreshes action-item data', async () => {
      const user = userEvent.setup()
      const confirmedActionItems: MeetingActionItems = {
        ...actionItems,
        action_items: [
          {
            ...actionItems.action_items[0],
            proposed_employee_id: 'employee-1',
            confirmed_employee_id: 'employee-1',
            employee_match_status: 'unique_match',
            ownership_status: 'confirmed',
          },
        ],
      }
      vi.mocked(getMeetingActionItems)
        .mockResolvedValueOnce(actionItems)
        .mockResolvedValueOnce(confirmedActionItems)

      renderReport()
      await user.selectOptions(
        await screen.findByRole('combobox', { name: 'Select owner' }),
        'employee-1',
      )
      await user.click(screen.getByRole('button', { name: 'Confirm Owner' }))

      expect(confirmActionItemOwner).toHaveBeenCalledWith(
        'meeting-current',
        'item-1',
        'employee-1',
        'organizer-1',
      )
      expect(await screen.findByText('Owner confirmed successfully.')).toBeInTheDocument()
      expect(screen.getByText('Confirmed')).toBeInTheDocument()
      expect(getMeetingActionItems).toHaveBeenCalledTimes(2)
    })

    it('reports the actual number of extracted action items', async () => {
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          ...actionItems.action_items,
          {
            ...actionItems.action_items[0],
            id: 'item-2',
            owner_name: 'Rahul',
            task_description: 'Review the notes.',
          },
        ],
      })
      renderReport()

      expect(await screen.findByText(
        'Extraction status: completed. 2 action items extracted.',
      )).toBeInTheDocument()
      expect(screen.queryByText('No action items were identified.')).not.toBeInTheDocument()
    })

    it('shows fallbacks for missing owner and deadline', async () => {
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          {
            ...actionItems.action_items[0],
            owner_name: null,
            employee_match_status: null,
            ownership_status: null,
            deadline_at: null,
            deadline_text: null,
          },
        ],
      })
      renderReport()

      expect(await screen.findByText('Unassigned')).toBeInTheDocument()
      expect(screen.getByText('Not available')).toBeInTheDocument()
      expect(screen.getByText('Unresolved')).toBeInTheDocument()
      expect(screen.getByText('Not specified')).toBeInTheDocument()
    })

    it('shows the loading, empty, and failed extraction states', async () => {
      let resolveItems!: (items: MeetingActionItems) => void
      vi.mocked(getMeetingActionItems).mockReturnValue(
        new Promise((resolve) => { resolveItems = resolve }),
      )
      renderReport()
      await screen.findByText('Quarterly planning')
      expect(await screen.findByText(/Loading meeting action items/)).toBeInTheDocument()

      resolveItems({ ...actionItems, action_items: [] })
      expect(await screen.findByText('No action items were identified.')).toBeInTheDocument()
      expect(screen.getByText(
        'Extraction status: completed. No action items were identified.',
      )).toBeInTheDocument()

      cleanup()
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        status: 'failed',
        error_message: 'Extraction failed.',
        action_items: [],
      })
      renderReport()
      expect(await screen.findByRole('alert')).toHaveTextContent('Extraction failed.')
      expect(screen.getByRole('button', { name: 'Retry Extraction' })).toBeInTheDocument()
    })

    it('triggers extraction and updates the displayed results', async () => {
      const user = userEvent.setup()
      vi.mocked(getMeetingActionItems).mockRejectedValue(
        new MeetingApiError('Meeting action items not found', 404),
      )
      vi.mocked(extractMeetingActionItems).mockResolvedValue(actionItems)
      renderReport()
      await user.click(await screen.findByRole('button', { name: 'Extract Action Items' }))

      expect(extractMeetingActionItems).toHaveBeenCalledWith('meeting-current', 'organizer-1')
      expect(await screen.findByText('Send the agenda.')).toBeInTheDocument()
    })

    it('prevents duplicate clicks while extraction is pending', async () => {
      const user = userEvent.setup()
      vi.mocked(getMeetingActionItems).mockRejectedValue(
        new MeetingApiError('Meeting action items not found', 404),
      )
      let resolveExtraction!: (items: MeetingActionItems) => void
      vi.mocked(extractMeetingActionItems).mockReturnValue(
        new Promise((resolve) => { resolveExtraction = resolve }),
      )
      renderReport()
      const extractButton = await screen.findByRole('button', { name: 'Extract Action Items' })
      await user.click(extractButton)

      expect(await screen.findByRole('button', { name: 'Extracting…' })).toBeDisabled()
      await user.click(screen.getByRole('button', { name: 'Extracting…' }))
      expect(extractMeetingActionItems).toHaveBeenCalledTimes(1)

      resolveExtraction(actionItems)
      expect(await screen.findByText('Send the agenda.')).toBeInTheDocument()
    })

    it('opens the referenced transcript segment from its source link', async () => {
      const user = userEvent.setup()
      const scrollToSegment = vi.fn()
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
        configurable: true,
        value: scrollToSegment,
      })
      renderReport()
      await user.click(
        await screen.findByRole('button', { name: 'Source transcript phrase: 0:00.92' }),
      )

      expect(await screen.findByRole('heading', { name: 'Meeting Transcript' })).toBeInTheDocument()
      expect(screen.getByTestId('current-location')).toHaveTextContent(
        '/meetings/meeting-current/report?transcript_id=transcript-1&segment=0',
      )
      expect(getMeetingTranscript).toHaveBeenCalledWith(
        'meeting-current',
        'organizer-1',
        'transcript-1',
      )
      const sourceSegment = document.getElementById('meeting-transcript-segment-0')
      expect(sourceSegment).toHaveClass('is-source-highlighted')
      await waitFor(() => expect(scrollToSegment).toHaveBeenCalled())
    })

    it('re-focuses the same segment on repeated clicks and follows different source links', async () => {
      const user = userEvent.setup()
      const scrolledSegmentIds: string[] = []
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
        configurable: true,
        value: function (this: HTMLElement) {
          scrolledSegmentIds.push(this.id)
        },
      })
      vi.mocked(getMeetingTranscript).mockResolvedValue({
        ...transcript,
        segments: [
          transcript.segments[0],
          {
            ...transcript.segments[0],
            start: 3,
            end: 5,
            text: 'Second transcript segment.',
          },
        ],
      })
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          actionItems.action_items[0],
          {
            ...actionItems.action_items[0],
            id: 'item-2',
            task_description: 'Review the second report.',
            source_segment_index: 1,
            source_start_seconds: 3,
            source_end_seconds: 5,
            source_quote_start_seconds: 3.4,
            source_quote_end_seconds: 4.8,
          },
        ],
      })
      renderReport()

      const firstSource = await screen.findByRole('button', {
        name: 'Source transcript phrase: 0:00.92',
      })
      await user.click(firstSource)
      await waitFor(() => expect(scrolledSegmentIds).toContain('meeting-transcript-segment-0'))
      const firstSegmentScrollCount = scrolledSegmentIds.length
      await user.click(firstSource)
      await waitFor(() => {
        expect(scrolledSegmentIds.length).toBeGreaterThan(firstSegmentScrollCount)
        expect(scrolledSegmentIds[scrolledSegmentIds.length - 1])
          .toBe('meeting-transcript-segment-0')
      })
      expect(document.getElementById('meeting-transcript-segment-0'))
        .toHaveClass('is-source-highlighted')

      const secondSource = screen.getByRole('button', {
        name: 'Source transcript phrase: 0:03.40',
      })
      const secondSegmentScrollCount = scrolledSegmentIds.length
      await user.click(secondSource)
      await waitFor(() => {
        expect(scrolledSegmentIds.length).toBeGreaterThan(secondSegmentScrollCount)
        expect(scrolledSegmentIds[scrolledSegmentIds.length - 1])
          .toBe('meeting-transcript-segment-1')
      })
      expect(screen.getByTestId('current-location')).toHaveTextContent(
        '/meetings/meeting-current/report?transcript_id=transcript-1&segment=1',
      )
      expect(document.getElementById('meeting-transcript-segment-1'))
        .toHaveClass('is-source-highlighted')

      const repeatedSecondSegmentScrollCount = scrolledSegmentIds.length
      await user.click(secondSource)
      await waitFor(() => {
        expect(scrolledSegmentIds.length).toBeGreaterThan(repeatedSecondSegmentScrollCount)
        expect(scrolledSegmentIds[scrolledSegmentIds.length - 1])
          .toBe('meeting-transcript-segment-1')
      })
    })

    it('shows a graceful message if the referenced transcript segment is missing', async () => {
      const user = userEvent.setup()
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          { ...actionItems.action_items[0], source_segment_index: 4 },
        ],
      })
      renderReport()
      await user.click(
        await screen.findByRole('button', { name: 'Source transcript phrase: 0:00.92' }),
      )

      expect(await screen.findByRole('alert')).toHaveTextContent(
        'The referenced transcript segment is not available.',
      )
    })

    it('navigates to the source meeting and exact transcript segment for other source records', async () => {
      const user = userEvent.setup()
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          {
            ...actionItems.action_items[0],
            meeting_id: 'meeting-source',
            transcript_id: 'transcript-source',
            source_segment_index: 2,
            source_quote_start_seconds: 13.44,
          },
        ],
      })

      renderReport()
      await user.click(
        await screen.findByRole('button', { name: 'Source transcript phrase: 0:13.44' }),
      )

      expect(screen.getByTestId('current-location')).toHaveTextContent(
        '/meetings/meeting-source/report?transcript_id=transcript-source&segment=2',
      )
      expect(getMeetingTranscript).toHaveBeenCalledWith(
        'meeting-source',
        'organizer-1',
        'transcript-source',
      )
    })

    it('shows and navigates to the source segment when an exact phrase timestamp is unavailable', async () => {
      const user = userEvent.setup()
      vi.mocked(getMeetingTranscript).mockResolvedValue({
        ...transcript,
        segments: [
          { ...transcript.segments[0], start: 8.25, end: 9.2 },
          {
            ...transcript.segments[0],
            start: 20.68,
            end: 27.3,
            text: 'test the IPI in points and fix existing bugs by 13 October.',
          },
        ],
      })
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          {
            ...actionItems.action_items[0],
            source_segment_index: 1,
            source_start_seconds: 20.68,
            source_end_seconds: 27.3,
            source_quote_start_seconds: null,
            source_quote_end_seconds: null,
          },
        ],
      })
      renderReport()

      expect(
        await screen.findByRole('button', {
          name: 'Source transcript segment starts at 0:20.68',
        }),
      ).toBeInTheDocument()
      await user.click(
        screen.getByRole('button', {
          name: 'Source transcript segment starts at 0:20.68',
        }),
      )
      expect(await screen.findByRole('heading', { name: 'Meeting Transcript' })).toBeInTheDocument()
      expect(
        document.querySelector('#meeting-transcript-segment-1 time'),
      ).toHaveTextContent('0:20.68–0:27.30')
      expect(screen.getByTestId('current-location')).toHaveTextContent(
        '/meetings/meeting-current/report?transcript_id=transcript-1&segment=1',
      )
      expect(getMeetingTranscript).toHaveBeenCalledWith(
        'meeting-current',
        'organizer-1',
        'transcript-1',
      )
      expect(document.getElementById('meeting-transcript-segment-1'))
        .toHaveClass('is-source-highlighted')
    })

    it('displays 0:00 when word timing confirms the source phrase starts at zero', async () => {
      vi.mocked(getMeetingActionItems).mockResolvedValue({
        ...actionItems,
        action_items: [
          {
            ...actionItems.action_items[0],
            source_quote_start_seconds: 0,
            source_quote_end_seconds: 1.3,
          },
        ],
      })
      renderReport()

      expect(
        await screen.findByRole('button', { name: 'Source transcript phrase: 0:00' }),
      ).toBeInTheDocument()
    })
  })

  it.each([
    ['pending', 'Transcript is pending.'],
    ['processing', 'Transcript is being processed.'],
  ])('renders %s transcript status', async (status, message) => {
    vi.mocked(getMeetingTranscript).mockResolvedValue({
      ...transcript,
      status,
      segments: [],
    })
    const user = userEvent.setup()
    renderReport()
    await user.click(await screen.findByRole('button', { name: 'View Transcript' }))

    expect(await screen.findByText(message)).toBeInTheDocument()
  })

  it('renders failure details as text instead of interpreting them as markup', async () => {
    vi.mocked(getMeetingTranscript).mockResolvedValue({
      ...transcript,
      status: 'failed',
      error_message: '<script>unsafe detail</script>',
      segments: [],
    })
    const user = userEvent.setup()
    renderReport()
    await user.click(await screen.findByRole('button', { name: 'View Transcript' }))

    const errorDetail = await screen.findByText('<script>unsafe detail</script>')
    expect(errorDetail.tagName).toBe('SPAN')
    expect(document.querySelector('script')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Transcription failed')
  })

  it('shows a clear state when no transcript exists', async () => {
    vi.mocked(getMeetingTranscript).mockRejectedValue(
      new MeetingApiError('Transcript not found', 404),
    )
    const user = userEvent.setup()
    renderReport()
    await user.click(await screen.findByRole('button', { name: 'View Transcript' }))

    expect(await screen.findByText('Transcript not available.')).toBeInTheDocument()
  })

  it('hides the transcript from the report action', async () => {
    const user = userEvent.setup()
    renderReport()
    await user.click(await screen.findByRole('button', { name: 'View Transcript' }))
    expect(
      await within(screen.getByLabelText('Meeting transcript')).findByText('Thank you very much.'),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Hide Transcript' }))
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Meeting Transcript' })).not.toBeInTheDocument()
    })
  })

  describe('MeetingReportPage minutes (Story 4.2)', () => {
    it('fetches and displays the persisted meeting minutes when requested by organizer', async () => {
      const user = userEvent.setup()
      renderReport()

      await user.click(await screen.findByRole('button', { name: 'View Minutes' }))

      expect(await screen.findByRole('heading', { name: 'Meeting Minutes' })).toBeInTheDocument()
      expect(getMeetingMinutes).toHaveBeenCalledWith('meeting-current', 'organizer-1')
      expect(screen.getByText('The team planned the next quarter.')).toBeInTheDocument()
      expect(screen.getByText('Start the pilot in May.')).toBeInTheDocument()
      expect(screen.getAllByText('Send the agenda.').length).toBeGreaterThanOrEqual(1)
      expect(screen.getByText('Pending Approval')).toBeInTheDocument()
      expect(screen.getAllByText('maya@example.com').length).toBeGreaterThanOrEqual(1)
    })

    it('shows Generate Meeting Minutes button when 404 and generates on click', async () => {
      vi.mocked(getMeetingMinutes).mockRejectedValueOnce(
        new MeetingApiError('Meeting minutes not found', 404),
      )
      const user = userEvent.setup()
      renderReport()

      await user.click(await screen.findByRole('button', { name: 'View Minutes' }))

      const generateBtn = await screen.findByRole('button', { name: /generate meeting minutes/i })
      expect(generateBtn).toBeInTheDocument()

      await user.click(generateBtn)
      expect(generateMeetingMinutes).toHaveBeenCalledWith('meeting-current', 'organizer-1')
      expect(await screen.findByText('The team planned the next quarter.')).toBeInTheDocument()
    })

    it('allows organizer to approve minutes', async () => {
      const user = userEvent.setup()
      renderReport()

      await user.click(await screen.findByRole('button', { name: 'View Minutes' }))
      expect(await screen.findByRole('heading', { name: 'Meeting Minutes' })).toBeInTheDocument()

      const approveBtn = screen.getByRole('button', { name: /approve minutes/i })
      await user.click(approveBtn)

      const confirmBtn = screen.getByRole('button', { name: /confirm approval/i })
      await user.click(confirmBtn)

      expect(approveMeetingMinutes).toHaveBeenCalledWith('meeting-current', 'organizer-1', undefined)
      expect(await screen.findByText(/meeting minutes approved successfully/i)).toBeInTheDocument()
    })

    it('allows organizer to send minutes only after approval', async () => {
      // Start with approved minutes
      vi.mocked(getMeetingMinutes).mockResolvedValue({
        ...minutes,
        status: 'approved',
        approved_by: 'organizer-1',
        approved_at: '2026-10-05T11:10:00Z',
      })

      const user = userEvent.setup()
      renderReport()

      await user.click(await screen.findByRole('button', { name: 'View Minutes' }))
      expect(await screen.findByRole('heading', { name: 'Meeting Minutes' })).toBeInTheDocument()

      const sendBtn = screen.getByRole('button', { name: /send minutes/i })
      expect(sendBtn).not.toBeDisabled()

      await user.click(sendBtn)
      expect(sendMeetingMinutes).toHaveBeenCalledWith('meeting-current', 'organizer-1', false)
      expect(await screen.findByText(/sent successfully/i)).toBeInTheDocument()
    })

    it('allows organizer to retry failed recipients', async () => {
      // Minutes with failed recipient
      vi.mocked(getMeetingMinutes).mockResolvedValue({
        ...minutes,
        status: 'approved',
        recipients: [
          {
            ...minutes.recipients[0],
            delivery_status: 'failed',
            error_message: 'Temporary delivery error',
          },
        ],
      })

      const user = userEvent.setup()
      renderReport()

      await user.click(await screen.findByRole('button', { name: 'View Minutes' }))
      expect(await screen.findByRole('heading', { name: 'Meeting Minutes' })).toBeInTheDocument()

      const retryBtn = screen.getByRole('button', { name: /retry failed deliveries/i })
      expect(retryBtn).toBeInTheDocument()

      await user.click(retryBtn)
      expect(sendMeetingMinutes).toHaveBeenCalledWith('meeting-current', 'organizer-1', true)
    })
  })
})
