// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it } from 'vitest'
import { MeetingSummaryPanel } from './MeetingSummaryPanel'
import type { MeetingSummary } from '../../types/meeting'

const summary: MeetingSummary = {
  id: 'summary-1',
  meeting_id: 'meeting-1',
  transcript_id: 'transcript-1',
  status: 'completed',
  overview: 'A persisted overview.',
  decisions: ['Decision one.'],
  discussion_topics: ['Topic one.'],
  open_questions: ['Question one?'],
  created_at: '2026-10-05T11:00:00Z',
  updated_at: '2026-10-05T11:01:00Z',
  completed_at: '2026-10-05T11:01:00Z',
  error_message: null,
}

afterEach(cleanup)

describe('MeetingSummaryPanel', () => {
  it('renders a loading state', () => {
    render(<MeetingSummaryPanel summary={null} loading />)

    expect(screen.getByRole('status')).toHaveTextContent('Loading meeting summary')
  })

  it('renders an unavailable state', () => {
    render(<MeetingSummaryPanel summary={null} loading={false} />)

    expect(screen.getByRole('status')).toHaveTextContent('Meeting summary not available.')
  })

  it('renders a safe retrieval error', () => {
    render(
      <MeetingSummaryPanel
        summary={null}
        loading={false}
        error="<script>unavailable</script>"
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('<script>unavailable</script>')
    expect(document.querySelector('script')).not.toBeInTheDocument()
  })

  it('renders the four sections of a completed summary', () => {
    render(<MeetingSummaryPanel summary={summary} loading={false} />)

    expect(screen.getByText('A persisted overview.')).toBeInTheDocument()
    expect(screen.getByText('Decision one.')).toBeInTheDocument()
    expect(screen.getByText('Topic one.')).toBeInTheDocument()
    expect(screen.getByText('Question one?')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Decisions' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Discussion Topics' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Open Questions' })).toBeInTheDocument()
  })

  it('renders failed-summary error details as text', () => {
    render(
      <MeetingSummaryPanel
        summary={{ ...summary, status: 'failed', error_message: '<script>failure</script>' }}
        loading={false}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('<script>failure</script>')
    expect(document.querySelector('script')).not.toBeInTheDocument()
  })
})
