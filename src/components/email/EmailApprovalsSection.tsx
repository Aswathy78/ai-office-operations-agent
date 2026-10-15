import { useCallback, useEffect, useState } from 'react'
import { Mail, RefreshCw } from 'lucide-react'
import {
  EMAIL_STATUS_POLL_MS,
  decideEmailDraft,
  getEmailApprovals,
  type EmailDraft,
} from '../../api/emailDrafts'
import type { TaskActor } from '../../api/tasks'
import { usePolling } from '../../hooks/usePolling'
import { EmailDraftPreview } from './EmailDraftPreview'

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(iso))
}

/** Approval queue for e-mails drafted from chat that exceed the recipient limit or go outside the company. */
export function EmailApprovalsSection({ actor }: { actor: TaskActor }) {
  const [filter, setFilter] = useState<'approval' | ''>('approval')
  const [drafts, setDrafts] = useState<EmailDraft[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [deciding, setDeciding] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, { tone: 'success' | 'error'; message: string }>>({})

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const fetched = await getEmailApprovals(filter, actor)
      // Keep e-mails decided on this page visible (with their feedback) after they leave the queue.
      setDrafts((current) => [
        ...fetched,
        ...current.filter((item) => item.approval_decision && !fetched.some((f) => f.id === item.id)),
      ])
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'E-mail approvals could not be loaded.')
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [filter, actor])

  useEffect(() => {
    void load()
  }, [load])

  // New requests, withdrawals and other approvers' decisions show up within 5 seconds.
  usePolling(() => load(true), EMAIL_STATUS_POLL_MS, !loading)

  async function decide(draft: EmailDraft, decision: 'approve' | 'reject') {
    setDeciding(draft.id)
    try {
      const updated = await decideEmailDraft(draft.id, decision, notes[draft.id] ?? '', actor)
      setDrafts((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      setFeedback((current) => ({
        ...current,
        [draft.id]: {
          tone: 'success',
          message: decision === 'approve'
            ? `Approved. ${draft.created_by_name} can now send the e-mail.`
            : `Rejected. ${draft.created_by_name} has been told it needs changes.`,
        },
      }))
    } catch (reason) {
      setFeedback((current) => ({
        ...current,
        [draft.id]: { tone: 'error', message: reason instanceof Error ? reason.message : 'The decision could not be saved.' },
      }))
    } finally {
      setDeciding(null)
    }
  }

  return (
    <div className="page-stack">
      <section className="task-workspace-card">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><Mail size={21} /></div>
          <div>
            <p className="eyebrow">Approvals</p>
            <h2>E-mail approvals</h2>
            <p>E-mails drafted from chat with many recipients or external recipients need your approval before the sender can send them.</p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <select
            aria-label="E-mail approval status"
            value={filter}
            onChange={(event) => setFilter(event.target.value as 'approval' | '')}
          >
            <option value="approval">Pending</option>
            <option value="">All e-mails</option>
          </select>
          <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error ? (
        <section className="meetings-error">
          <div>
            <h3>E-mail approvals unavailable</h3>
            <p>{error}</p>
          </div>
        </section>
      ) : loading ? (
        <div className="meetings-loading" role="status">Loading e-mail approvals...</div>
      ) : drafts.length === 0 ? (
        <section className="meetings-empty">
          <div className="state-icon" aria-hidden="true"><Mail size={28} /></div>
          <h3>No {filter ? 'pending ' : ''}e-mails</h3>
          <p>E-mails that need approval will appear here.</p>
        </section>
      ) : (
        <ul className="approval-list">
          {drafts.map((draft) => (
            <li key={draft.id} className="approval-card">
              <p className="task-manage-hint">
                Requested by {draft.created_by_name} ({draft.created_by_role}) on {formatTimestamp(draft.updated_at)}
                {draft.decided_at && draft.approval_decision
                  ? ` · ${draft.approval_decision === 'approved' ? 'Approved' : 'Rejected'} by ${draft.decided_by} on ${formatTimestamp(draft.decided_at)}`
                  : ''}
              </p>
              <EmailDraftPreview draft={draft} actor={actor} readOnly />
              {feedback[draft.id] && (
                <p
                  className={`task-manage-feedback ${feedback[draft.id].tone}`}
                  role={feedback[draft.id].tone === 'error' ? 'alert' : 'status'}
                >
                  {feedback[draft.id].message}
                </p>
              )}
              {draft.status === 'approval' && draft.created_by_id !== actor.id && (
                <div className="approval-actions">
                  <input
                    type="text"
                    aria-label={`Decision note for ${draft.subject}`}
                    placeholder="Note (optional)"
                    value={notes[draft.id] ?? ''}
                    onChange={(event) => setNotes((current) => ({ ...current, [draft.id]: event.target.value }))}
                    disabled={deciding === draft.id}
                  />
                  <button className="button primary" type="button" onClick={() => void decide(draft, 'approve')} disabled={deciding === draft.id}>
                    Approve e-mail
                  </button>
                  <button className="button secondary" type="button" onClick={() => void decide(draft, 'reject')} disabled={deciding === draft.id}>
                    Reject e-mail
                  </button>
                </div>
              )}
              {draft.status === 'approval' && draft.created_by_id === actor.id && (
                <p className="task-manage-pending">You cannot approve your own e-mail.</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
