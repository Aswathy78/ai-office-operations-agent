// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import {
  getMeetingReport,
  getMeetingSummary,
  getMeetingTranscript,
  MeetingApiError,
  updateMeetingTranscript,
} from '../api/meetings'
import { MeetingReportPage } from './MeetingReportPage'
import type { MeetingReport, MeetingSummary, MeetingTranscript } from '../types/meeting'

const { authValue } = vi.hoisted(() => ({
  authValue: { user: { id: 'organizer-1' } },
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
  getMeetingSummary: vi.fn(),
  getMeetingTranscript: vi.fn(),
  getMeetingTranscriptCorrections: vi.fn(),
  updateMeetingTranscript: vi.fn(),
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

function renderReport() {
  return render(
    <MemoryRouter initialEntries={['/meetings/meeting-current/report']}>
      <Routes>
        <Route path="/meetings/:meetingId/report" element={<MeetingReportPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(getMeetingReport).mockResolvedValue(report)
  vi.mocked(getMeetingSummary).mockResolvedValue(summary)
  vi.mocked(getMeetingTranscript).mockResolvedValue(transcript)
  vi.mocked(updateMeetingTranscript).mockResolvedValue(transcript)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MeetingReportPage transcript', () => {
  it('shows the transcript action while preserving report content', async () => {
    renderReport()

    expect(await screen.findByText('Quarterly planning')).toBeInTheDocument()
    expect(screen.getByText('Attendance Log')).toBeInTheDocument()
    expect(screen.getByText('Maya')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View Transcript' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View Summary' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Meeting Summary' })).not.toBeInTheDocument()
    expect(getMeetingSummary).not.toHaveBeenCalled()
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
    expect(screen.getByText('Thank you very much.')).toBeInTheDocument()
    expect(within(screen.getByLabelText('Meeting transcript')).getByText('Maya')).toBeInTheDocument()
    expect(screen.getByText('0:00–0:03')).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: 'Search transcript' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit segment 1' })).toBeInTheDocument()
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
    expect(await screen.findByText('Thank you very much.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Hide Transcript' }))
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Meeting Transcript' })).not.toBeInTheDocument()
    })
  })
})
