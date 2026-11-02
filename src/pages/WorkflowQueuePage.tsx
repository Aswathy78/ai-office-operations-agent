import { useCallback, useEffect, useState } from 'react'
import { Ban, OctagonAlert, Play, RefreshCw, RotateCcw } from 'lucide-react'
import {
  actOnDeadLetter,
  getDeadLetterDetail,
  getDeadLetterQueue,
  type DeadLetterAction,
  type DeadLetterDetail,
  type DeadLetterEntry,
  type DeadLetterQueue,
  type DeadLetterStatus,
} from '../api/workflows'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'

type Filter = DeadLetterStatus | 'all'

const FILTERS: { value: Filter; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'retried', label: 'Retried' },
  { value: 'resumed', label: 'Resumed' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'all', label: 'All' },
]

const EVENT_LABELS: Record<string, string> = {
  step_completed: 'Step completed',
  retry_scheduled: 'Retry scheduled',
  dead_lettered: 'Moved to the dead-letter queue',
  lease_expired: 'Worker stopped responding',
  manual_retry: 'Administrator retried from the start',
  manual_resume: 'Administrator resumed from the checkpoint',
  manual_cancel: 'Administrator cancelled the run',
}

const ACTIONS: {
  action: DeadLetterAction
  label: string
  hint: string
  icon: typeof Play
  confirm: string
}[] = [
  {
    action: 'retry',
    label: 'Retry from start',
    hint: 'Run every step again with a fresh set of five attempts.',
    icon: RotateCcw,
    confirm: 'Run this workflow again from the first step?',
  },
  {
    action: 'resume',
    label: 'Resume from checkpoint',
    hint: 'Keep the steps that already completed and run the rest.',
    icon: Play,
    confirm: 'Resume this workflow from its last checkpoint?',
  },
  {
    action: 'cancel',
    label: 'Cancel run',
    hint: 'Give up on this workflow. It will not run again.',
    icon: Ban,
    confirm: 'Cancel this workflow run? It will not run again.',
  },
]

/** The API sends UTC; some timestamps arrive without a zone, which a browser would
 *  read as local time. Treat a missing zone as UTC. */
function parseUtc(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
}

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
    parseUtc(iso),
  )
}

function humanize(value: string): string {
  return value.replaceAll('_', ' ')
}

function statusTone(status: string): string {
  if (status === 'open' || status === 'failed') return 'danger'
  if (status === 'cancelled' || status === 'completed') return 'muted'
  return 'warning'
}

export function WorkflowQueuePage() {
  const { user } = useAuth()
  const [filter, setFilter] = useState<Filter>('open')
  const [queue, setQueue] = useState<DeadLetterQueue | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<DeadLetterDetail | null>(null)
  const [detailError, setDetailError] = useState('')
  const [note, setNote] = useState('')
  const [acting, setActing] = useState<DeadLetterAction | null>(null)
  const [feedback, setFeedback] = useState<{ tone: 'success' | 'error'; message: string } | null>(
    null,
  )

  const isAdmin = user?.role === 'admin'

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setQueue(await getDeadLetterQueue(filter))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The queue could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    if (isAdmin) void load()
  }, [isAdmin, load])

  const openDetail = useCallback(async (runId: string) => {
    setSelected(runId)
    setDetail(null)
    setDetailError('')
    setFeedback(null)
    setNote('')
    try {
      setDetail(await getDeadLetterDetail(runId))
    } catch (reason) {
      setDetailError(reason instanceof Error ? reason.message : 'Details could not be loaded.')
    }
  }, [])

  async function act(action: DeadLetterAction, confirmText: string) {
    if (!selected || !window.confirm(confirmText)) return
    setActing(action)
    setFeedback(null)
    try {
      const updated = await actOnDeadLetter(selected, action, note)
      setDetail(updated)
      setNote('')
      setFeedback({
        tone: 'success',
        message:
          action === 'cancel'
            ? 'The workflow run was cancelled.'
            : action === 'retry'
              ? 'The workflow was queued to run again from the start.'
              : 'The workflow was queued to resume from its last checkpoint.',
      })
      await load()
    } catch (reason) {
      setFeedback({
        tone: 'error',
        message: reason instanceof Error ? reason.message : 'The action could not be applied.',
      })
      // The run may have been handled elsewhere; show what it looks like now.
      void getDeadLetterDetail(selected).then(setDetail).catch(() => undefined)
      void load()
    } finally {
      setActing(null)
    }
  }

  if (!isAdmin) return <AccessDenied />

  const entries = queue?.entries ?? []
  const counts = queue?.counts
  const latest: DeadLetterEntry | undefined = detail?.dead_letters[0]
  const actionable = detail?.workflow.status === 'failed' && latest?.status === 'open'

  return (
    <div className="task-workspace">
      <section className="task-workspace-card" aria-label="Workflow dead-letter queue">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><OctagonAlert size={21} /></div>
          <div>
            <p className="eyebrow">Workflow monitoring</p>
            <h2>Dead-letter queue</h2>
            <p>
              Workflow runs that failed after five automatic attempts. Nothing here is hidden or
              shown as successful: review the error, then retry, resume or cancel.
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <select
            aria-label="Dead-letter status filter"
            value={filter}
            onChange={(event) => setFilter(event.target.value as Filter)}
          >
            {FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
                {counts && option.value !== 'all' ? ` (${counts[option.value]})` : ''}
              </option>
            ))}
          </select>
          <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error && (
        <div className="meetings-error" role="alert">
          <p>{error}</p>
        </div>
      )}

      {loading && !queue ? (
        <div className="meetings-loading" role="status">Loading the dead-letter queue…</div>
      ) : entries.length === 0 ? (
        <div className="meetings-empty" role="status">
          <h3>{filter === 'open' ? 'No failed workflows' : 'No entries'}</h3>
          <p>
            {filter === 'open'
              ? 'Every workflow run is healthy. Failed runs appear here after their retries are used up.'
              : 'Nothing matches this filter.'}
          </p>
        </div>
      ) : (
        <div className="task-table-wrap">
          <table className="task-table" aria-label="Dead-lettered workflow runs">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Failed step</th>
                <th>Last error</th>
                <th>Attempts</th>
                <th>Moved to queue</th>
                <th>Status</th>
                <th><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id} className={selected === entry.run_id ? 'task-row-selected' : undefined}>
                  <td>
                    <strong>{entry.workflow_label}</strong>
                    {entry.meeting_title && <small>{entry.meeting_title}</small>}
                  </td>
                  <td>{entry.failed_step_key ? humanize(entry.failed_step_key) : '—'}</td>
                  <td className="email-status-error">{entry.error_message ?? '—'}</td>
                  <td>{entry.attempt_count} of {entry.max_attempts}</td>
                  <td>{formatTime(entry.created_at)}</td>
                  <td>
                    <span className={`status-pill ${statusTone(entry.status)}`}>{humanize(entry.status)}</span>
                  </td>
                  <td>
                    <button
                      className="button secondary"
                      type="button"
                      aria-label={`View details for ${entry.workflow_label}`}
                      onClick={() => void openDetail(entry.run_id)}
                    >
                      Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <section className="task-workspace-card workflow-dlq-detail" aria-label="Workflow run details">
          {detailError ? (
            <p className="task-manage-feedback error" role="alert">{detailError}</p>
          ) : !detail ? (
            <p role="status">Loading details…</p>
          ) : (
            <div>
              <p className="eyebrow">Run {detail.workflow.id}</p>
              <h3>
                {latest?.workflow_label ?? humanize(detail.workflow.workflow_type)}
                {detail.meeting_title ? ` — ${detail.meeting_title}` : ''}
              </h3>
              <p>
                Run status{' '}
                <span className={`status-pill ${statusTone(detail.workflow.status)}`}>
                  {humanize(detail.workflow.status)}
                </span>{' '}
                · {detail.workflow.attempt_count} of {detail.workflow.max_attempts ?? 5} attempts used
                {latest && latest.notified_count === 0 && latest.status === 'open'
                  ? ' · no administrator could be notified'
                  : ''}
              </p>
              {detail.workflow.last_error && (
                <p className="email-status-error" role="status">{detail.workflow.last_error}</p>
              )}

              <h4>Steps</h4>
              <ol className="workflow-step-list">
                {detail.workflow.steps.map((step) => (
                  <li key={step.id}>
                    <span>{humanize(step.step_key)}</span>
                    <span className={`status-pill ${statusTone(step.status)}`}>{humanize(step.status)}</span>
                    {step.last_error && <small className="email-status-error">{step.last_error}</small>}
                  </li>
                ))}
              </ol>

              <h4>Execution history</h4>
              {detail.events.length === 0 ? (
                <p>No history was recorded for this run.</p>
              ) : (
                <ol className="workflow-history" aria-label="Execution history">
                  {detail.events.map((event) => (
                    <li key={event.id}>
                      <strong>{EVENT_LABELS[event.event_type] ?? humanize(event.event_type)}</strong>
                      <small>
                        {formatTime(event.created_at)}
                        {event.step_key ? ` · ${humanize(event.step_key)}` : ''}
                        {event.attempt_number ? ` · attempt ${event.attempt_number}` : ''}
                        {event.retry_delay_seconds != null
                          ? ` · next try in ${Math.round(event.retry_delay_seconds)}s`
                          : ''}
                        {event.actor_id ? ` · by ${event.actor_id}` : ''}
                      </small>
                      {event.message && <span>{event.message}</span>}
                      {event.error && <span className="email-status-error">{event.error}</span>}
                    </li>
                  ))}
                </ol>
              )}

              <h4>Job details</h4>
              <pre className="workflow-payload" aria-label="Job payload">
                {JSON.stringify(detail.payload, null, 2)}
              </pre>

              {latest && latest.status !== 'open' && (
                <p role="status">
                  Resolved as <strong>{latest.status}</strong> on {formatTime(latest.resolved_at)}
                  {latest.resolved_by ? ` by ${latest.resolved_by}` : ''}
                  {latest.resolution_note ? ` — “${latest.resolution_note}”` : ''}.
                </p>
              )}

              {feedback && (
                <p
                  className={`task-manage-feedback ${feedback.tone}`}
                  role={feedback.tone === 'error' ? 'alert' : 'status'}
                >
                  {feedback.message}
                </p>
              )}

              {actionable ? (
                <div className="workflow-dlq-actions">
                  <label htmlFor="dlq-note">Note (optional)</label>
                  <textarea
                    id="dlq-note"
                    rows={2}
                    maxLength={1000}
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="Why you are retrying, resuming or cancelling"
                  />
                  <div className="task-workspace-actions">
                    {ACTIONS.map(({ action, label, hint, icon: Icon, confirm }) => (
                      <button
                        key={action}
                        className={`button ${action === 'cancel' ? 'secondary' : 'primary'}`}
                        type="button"
                        title={hint}
                        disabled={acting !== null}
                        onClick={() => void act(action, confirm)}
                      >
                        <Icon size={16} aria-hidden="true" /> {acting === action ? 'Working…' : label}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <p>This run is no longer waiting for an administrator.</p>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
