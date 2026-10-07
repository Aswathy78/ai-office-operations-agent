// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getMeetingTranscript,
  getMeetingTranscriptCorrections,
  MeetingApiError,
  updateMeetingTranscript,
} from '../../api/meetings'
import type { MeetingTranscript, TranscriptCorrection } from '../../types/meeting'
import { TranscriptPanel } from './TranscriptPanel'

vi.mock('../../api/meetings', () => ({
  MeetingApiError: class MeetingApiError extends Error {
    constructor(message: string, readonly status: number) {
      super(message)
    }
  },
  getMeetingTranscript: vi.fn(),
  getMeetingTranscriptCorrections: vi.fn(),
  updateMeetingTranscript: vi.fn(),
}))

const transcript: MeetingTranscript = {
  id: 'transcript-1',
  meeting_id: 'meeting-1',
  recording_id: 'recording-1',
  status: 'completed',
  language: 'en',
  transcript_text: 'Welcome. This is the meeting.',
  segments: [
    {
      start: 2,
      end: 3,
      text: 'Welcome.',
      speaker: 'Alice',
      flagged_for_correction: false,
    },
    {
      start: 65,
      end: 68,
      text: 'This is the meeting.',
      speaker: 'Bob',
      flagged_for_correction: true,
    },
  ],
  error_message: null,
  created_at: '2026-10-05T10:00:00Z',
  processing_started_at: null,
  completed_at: '2026-10-05T10:01:00Z',
}

const correction: TranscriptCorrection = {
  id: 'correction-1',
  meeting_id: 'meeting-1',
  transcript_id: 'transcript-1',
  segment_index: 0,
  original_text: 'Welcome.',
  corrected_text: 'Welcome everyone.',
  original_speaker: 'Alice',
  corrected_speaker: 'Alicia',
  editor_id: 'organizer-1',
  created_at: '2026-10-05T10:02:00Z',
}

function renderPanel(isOrganizer = true) {
  return render(
    <TranscriptPanel
      meetingId="meeting-1"
      organizerId="organizer-1"
      isOrganizer={isOrganizer}
      isOpen
      onClose={vi.fn()}
    />,
  )
}

function pending<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.mocked(getMeetingTranscript).mockResolvedValue(transcript)
  vi.mocked(updateMeetingTranscript).mockResolvedValue({
    ...transcript,
    transcript_text: 'Welcome everyone. This is the meeting.',
    segments: [
      { ...transcript.segments[0], text: 'Welcome everyone.', speaker: 'Alicia' },
      transcript.segments[1],
    ],
  })
  vi.mocked(getMeetingTranscriptCorrections).mockResolvedValue([correction])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TranscriptPanel', () => {
  it('shows transcript loading, then renders speakers and formatted timestamps', async () => {
    const request = pending<MeetingTranscript>()
    vi.mocked(getMeetingTranscript).mockReturnValue(request.promise)

    renderPanel()
    expect(screen.getByRole('status')).toHaveTextContent('Loading transcript')

    await act(async () => request.resolve(transcript))

    expect(await screen.findByText('Welcome.')).toBeInTheDocument()
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
    expect(screen.getByText('0:02–0:03')).toBeInTheDocument()
    expect(screen.getByText('1:05–1:08')).toBeInTheDocument()
    expect(screen.getByText('Flagged for correction')).toBeInTheDocument()
  })

  it('shows processing and failed states without rendering transcript text', async () => {
    vi.mocked(getMeetingTranscript).mockResolvedValue({
      ...transcript,
      status: 'processing',
      segments: [],
    })
    const { rerender } = renderPanel(false)
    expect(await screen.findByText('Transcript is being processed.')).toBeInTheDocument()
    expect(screen.queryByText('Welcome.')).not.toBeInTheDocument()

    vi.mocked(getMeetingTranscript).mockResolvedValue({
      ...transcript,
      status: 'failed',
      error_message: '<script>unsafe detail</script>',
      segments: [],
    })
    rerender(
      <TranscriptPanel
        meetingId="meeting-2"
        organizerId="organizer-1"
        isOrganizer={false}
        isOpen
        onClose={vi.fn()}
      />,
    )
    expect(await screen.findByText(/Transcription failed/)).toBeInTheDocument()
    expect(screen.getByText('<script>unsafe detail</script>')).toBeInTheDocument()
    expect(document.querySelector('script')).not.toBeInTheDocument()
  })

  it('shows pending status separately from processing', async () => {
    vi.mocked(getMeetingTranscript).mockResolvedValue({
      ...transcript,
      status: 'pending',
      segments: [],
    })
    renderPanel(false)
    expect(await screen.findByText('Transcript is pending.')).toBeInTheDocument()
  })

  it('shows a friendly unavailable state when no transcript exists', async () => {
    vi.mocked(getMeetingTranscript).mockRejectedValue(
      new MeetingApiError('Transcript not found', 404),
    )
    renderPanel(false)
    expect(await screen.findByText('Transcript not available.')).toBeInTheDocument()
  })

  it('filters segments case-insensitively and reports no search matches', async () => {
    const user = userEvent.setup()
    renderPanel(false)
    await screen.findByText('Welcome.')

    await user.type(screen.getByRole('searchbox', { name: 'Search transcript' }), 'MEETING')
    expect(screen.getByText('This is the meeting.')).toBeInTheDocument()
    expect(screen.queryByText('Welcome.')).not.toBeInTheDocument()
    expect(screen.getByText('1:05–1:08')).toBeInTheDocument()

    await user.clear(screen.getByRole('searchbox', { name: 'Search transcript' }))
    await user.type(screen.getByRole('searchbox', { name: 'Search transcript' }), 'no match')
    expect(screen.getByRole('status')).toHaveTextContent('No transcript segments match your search.')
  })

  it('allows organizer to edit text and speaker, then saves text and speaker only', async () => {
    const user = userEvent.setup()
    renderPanel()
    await screen.findByText('Welcome.')
    await user.click(screen.getByRole('button', { name: 'Edit segment 1' }))

    const textInput = screen.getByRole('textbox', { name: 'Edit transcript text for segment 1' })
    const speakerInput = screen.getByRole('textbox', { name: 'Edit speaker for segment 1' })
    await user.clear(textInput)
    await user.type(textInput, 'Welcome everyone.')
    await user.clear(speakerInput)
    await user.type(speakerInput, 'Alicia')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(updateMeetingTranscript).toHaveBeenCalledWith(
        'meeting-1',
        'organizer-1',
        [
          { text: 'Welcome everyone.', speaker: 'Alicia' },
          { text: 'This is the meeting.', speaker: 'Bob' },
        ],
      )
    })
    expect(await screen.findByText('Welcome everyone.')).toBeInTheDocument()
    expect(screen.getByText('Alicia')).toBeInTheDocument()
  })

  it('keeps the confirmed transcript while saving and updates only after success', async () => {
    const user = userEvent.setup()
    const request = pending<MeetingTranscript>()
    vi.mocked(updateMeetingTranscript).mockReturnValue(request.promise)
    renderPanel()
    await screen.findByText('Welcome.')
    await user.click(screen.getByRole('button', { name: 'Edit segment 1' }))
    const textInput = screen.getByRole('textbox', { name: 'Edit transcript text for segment 1' })
    await user.clear(textInput)
    await user.type(textInput, 'Edited welcome.')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: 'Edit transcript text for segment 1' }))
      .toHaveValue('Edited welcome.')
    expect(screen.getByText('This is the meeting.')).toBeInTheDocument()
    await act(async () => request.resolve({
      ...transcript,
      transcript_text: 'Edited welcome. This is the meeting.',
      segments: [{ ...transcript.segments[0], text: 'Edited welcome.' }, transcript.segments[1]],
    }))
    expect(await screen.findByText('Edited welcome.')).toBeInTheDocument()
  })

  it('cancels an edit without changing the transcript', async () => {
    const user = userEvent.setup()
    renderPanel()
    await screen.findByText('Welcome.')
    await user.click(screen.getByRole('button', { name: 'Edit segment 1' }))
    const textInput = screen.getByRole('textbox', { name: 'Edit transcript text for segment 1' })
    await user.clear(textInput)
    await user.type(textInput, 'Cancelled text')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.getByText('Welcome.')).toBeInTheDocument()
    expect(updateMeetingTranscript).not.toHaveBeenCalled()
  })

  it('shows correction history and its empty state only to the organizer', async () => {
    const user = userEvent.setup()
    const { rerender } = renderPanel()
    await screen.findByText('Welcome.')
    await user.click(screen.getByRole('button', { name: 'Corrections' }))

    const history = await screen.findByRole('region', { name: 'Transcript correction history' })
    expect(within(history).getByText('Segment 1')).toBeInTheDocument()
    expect(within(history).getByText('Original text')).toBeInTheDocument()
    expect(within(history).getByText('Welcome everyone.')).toBeInTheDocument()
    expect(within(history).getByText('Alicia')).toBeInTheDocument()
    expect(within(history).getByText('organizer-1')).toBeInTheDocument()
    expect(within(history).getByText(/5 Oct 2026/)).toBeInTheDocument()
    expect(getMeetingTranscriptCorrections).toHaveBeenCalledWith('meeting-1', 'organizer-1')

    await user.click(screen.getByRole('button', { name: 'Transcript' }))
    vi.mocked(getMeetingTranscriptCorrections).mockResolvedValue([])
    await user.click(screen.getByRole('button', { name: 'Corrections' }))
    expect(await screen.findByText('No transcript corrections yet.')).toBeInTheDocument()

    const guestView = renderPanel(false)
    await screen.findAllByText('Welcome.')
    expect(screen.queryByRole('button', { name: 'Corrections' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit segment 1' })).not.toBeInTheDocument()
    guestView.unmount()
  })

  it('handles transcript loading, saving, and correction-history failures safely', async () => {
    const user = userEvent.setup()
    vi.mocked(getMeetingTranscript).mockRejectedValue(new Error('Internal stack trace detail'))
    const { unmount } = renderPanel()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Unable to load the transcript. Check your connection and try again.',
    )
    expect(screen.queryByText('Internal stack trace detail')).not.toBeInTheDocument()

    unmount()
    vi.mocked(getMeetingTranscript).mockResolvedValue(transcript)
    vi.mocked(updateMeetingTranscript).mockRejectedValue(new Error('Internal stack trace detail'))
    renderPanel()
    await screen.findByText('Welcome.')
    await user.click(screen.getByRole('button', { name: 'Edit segment 1' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Unable to save transcript changes. Please try again.'))
      .toBeInTheDocument()
    expect(screen.queryByText('Internal stack trace detail')).not.toBeInTheDocument()

    vi.mocked(getMeetingTranscriptCorrections).mockRejectedValue(new Error('stack trace'))
    await user.click(screen.getByRole('button', { name: 'Corrections' }))
    expect(await screen.findByText(/Unable to load correction history/)).toBeInTheDocument()
    expect(screen.queryByText('stack trace')).not.toBeInTheDocument()
  })
})
