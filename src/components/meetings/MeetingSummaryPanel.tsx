import { FileText } from 'lucide-react'
import type { MeetingSummary } from '../../types/meeting'

interface MeetingSummaryPanelProps {
  summary: MeetingSummary | null
  loading: boolean
  error?: string
}

function SummaryItems({ items }: { items: string[] | null }) {
  return items?.length ? (
    <ul>
      {items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
    </ul>
  ) : (
    <p>None identified.</p>
  )
}

export function MeetingSummaryPanel({
  summary,
  loading,
  error = '',
}: MeetingSummaryPanelProps) {
  return (
    <section
      className="report-summary"
      aria-labelledby="meeting-summary-title"
      style={{
        background: 'var(--surface)',
        padding: '1.5rem',
        borderRadius: '8px',
        border: '1px solid var(--border)',
      }}
    >
      <h2
        id="meeting-summary-title"
        style={{ marginTop: 0, display: 'flex', alignItems: 'center', gap: '0.5rem' }}
      >
        <FileText size={18} aria-hidden="true" /> Meeting Summary
      </h2>

      {loading ? (
        <p role="status">Loading meeting summary…</p>
      ) : error ? (
        <div role="alert">
          <p>Unable to load meeting summary.</p>
          <p>{error}</p>
        </div>
      ) : !summary ? (
        <p role="status">Meeting summary not available.</p>
      ) : summary.status === 'pending' ? (
        <p role="status">Meeting summary is pending.</p>
      ) : summary.status === 'processing' ? (
        <p role="status">Meeting summary is being processed.</p>
      ) : summary.status === 'failed' ? (
        <div role="alert">
          <p>Meeting summary generation failed.</p>
          {summary.error_message && <p>{summary.error_message}</p>}
        </div>
      ) : (
        <div className="meeting-summary-sections">
          <section aria-labelledby="meeting-summary-overview">
            <h3 id="meeting-summary-overview">Overview</h3>
            <p>{summary.overview || 'No overview available.'}</p>
          </section>
          <section aria-labelledby="meeting-summary-decisions">
            <h3 id="meeting-summary-decisions">Decisions</h3>
            <SummaryItems items={summary.decisions} />
          </section>
          <section aria-labelledby="meeting-summary-discussion-topics">
            <h3 id="meeting-summary-discussion-topics">Discussion Topics</h3>
            <SummaryItems items={summary.discussion_topics} />
          </section>
          <section aria-labelledby="meeting-summary-open-questions">
            <h3 id="meeting-summary-open-questions">Open Questions</h3>
            <SummaryItems items={summary.open_questions} />
          </section>
        </div>
      )}
    </section>
  )
}
