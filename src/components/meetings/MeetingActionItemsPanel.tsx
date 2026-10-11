import { ClipboardList, RefreshCw } from 'lucide-react'
import type { MeetingActionItem, MeetingActionItems } from '../../types/meeting'

interface MeetingActionItemsPanelProps {
  extraction: MeetingActionItems | null
  loading: boolean
  triggering: boolean
  error?: string
  onExtract: () => void
  onOpenSource: (item: MeetingActionItem) => void
}

function formatTimestamp(seconds: number): string {
  const centiseconds = Math.round(Math.max(0, seconds) * 100)
  const wholeSeconds = Math.floor(centiseconds / 100)
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainingSeconds = wholeSeconds % 60
  const fractionalCentiseconds = centiseconds % 100
  const secondsLabel = `${String(remainingSeconds).padStart(2, '0')}${
    fractionalCentiseconds > 0
      ? `.${String(fractionalCentiseconds).padStart(2, '0')}`
      : ''
  }`
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${secondsLabel}`
  }
  return `${minutes}:${secondsLabel}`
}

function statusMessage(
  status: MeetingActionItems['status'],
  actionItemCount: number,
): string {
  switch (status) {
    case 'pending':
      return 'Action item extraction is pending.'
    case 'processing':
      return 'Action items are being extracted.'
    case 'failed':
      return 'Action item extraction failed.'
    case 'completed': {
      if (actionItemCount === 0) return 'No action items were identified.'
      const itemLabel = actionItemCount === 1 ? 'action item' : 'action items'
      return `${actionItemCount} ${itemLabel} extracted.`
    }
  }
}

export function MeetingActionItemsPanel({
  extraction,
  loading,
  triggering,
  error = '',
  onExtract,
  onOpenSource,
}: MeetingActionItemsPanelProps) {
  const status = extraction?.status
  const canExtract = !loading && !triggering && status !== 'processing'

  return (
    <section
      className="report-summary meeting-action-items"
      aria-labelledby="meeting-action-items-title"
      style={{
        background: 'var(--surface)',
        padding: '1.5rem',
        borderRadius: '8px',
        border: '1px solid var(--border)',
      }}
    >
      <h2
        id="meeting-action-items-title"
        style={{ marginTop: 0, display: 'flex', alignItems: 'center', gap: '0.5rem' }}
      >
        <ClipboardList size={18} aria-hidden="true" /> Meeting Action Items
      </h2>

      <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', flexWrap: 'wrap' }}>
        {status && (
          <p role="status">
            Extraction status: {status}. {statusMessage(status, extraction?.action_items.length ?? 0)}
          </p>
        )}
        {(!status || status === 'failed') && (
          <button
            className="button secondary"
            type="button"
            onClick={onExtract}
            disabled={!canExtract}
          >
            <RefreshCw size={15} aria-hidden="true" />
            {triggering ? 'Extracting…' : status === 'failed' ? 'Retry Extraction' : 'Extract Action Items'}
          </button>
        )}
      </div>

      {loading ? (
        <p role="status">Loading meeting action items…</p>
      ) : error ? (
        <div role="alert">
          <p>Unable to load meeting action items.</p>
          <p>{error}</p>
          <button className="button secondary" type="button" onClick={onExtract} disabled={!canExtract}>
            Retry
          </button>
        </div>
      ) : extraction?.status === 'failed' ? (
        <div role="alert">
          {extraction.error_message && <p>{extraction.error_message}</p>}
        </div>
      ) : extraction?.status === 'completed' && extraction.action_items.length > 0 ? (
        <ul className="meeting-action-item-list" style={{ display: 'grid', gap: '1rem', paddingLeft: '1.25rem' }}>
          {extraction.action_items.map((item) => (
            <li key={item.id}>
              <article>
                <p><strong>{item.task_description}</strong></p>
                <dl>
                  <div><dt>Owner</dt><dd>{item.owner_name || 'Unassigned'}</dd></div>
                  <div>
                    <dt>Deadline</dt>
                    <dd>
                      {item.deadline_text || (item.deadline_at
                        ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(item.deadline_at))
                        : 'Not specified')}
                    </dd>
                  </div>
                  <div><dt>Priority</dt><dd>{item.priority}</dd></div>
                  <div><dt>Confidence</dt><dd>{Math.round(item.confidence * 100)}%</dd></div>
                </dl>
                <button
                  className="meet-transcript-action"
                  type="button"
                  onClick={() => onOpenSource(item)}
                >
                  {item.source_quote_start_seconds === null
                    ? `Source transcript segment starts at ${formatTimestamp(item.source_start_seconds)}`
                    : `Source transcript phrase: ${formatTimestamp(item.source_quote_start_seconds)}`}
                </button>
                {item.source_quote && <blockquote>{item.source_quote}</blockquote>}
              </article>
            </li>
          ))}
        </ul>
      ) : extraction?.status === 'completed' ? (
        <p role="status">No action items were identified.</p>
      ) : null}
    </section>
  )
}
