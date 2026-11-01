import { useCallback, useEffect, useState } from 'react'
import { Bot, RefreshCw } from 'lucide-react'
import {
  cancelAgentApproval,
  decideAgentApproval,
  getAgentApprovals,
  type AgentActionApproval,
  type AgentApprovalStatus,
} from '../../api/assistantApprovals'
import { usePolling } from '../../hooks/usePolling'

const POLL_MS = 5000

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(iso))
}

const STATUS_LABELS: Record<AgentApprovalStatus, string> = {
  pending: 'Approval required',
  executed: 'Approved and done',
  rejected: 'Rejected',
  cancelled: 'Cancelled',
  expired: 'Expired',
  failed: 'Could not run',
}

function statusTone(status: AgentApprovalStatus): string {
  if (status === 'executed') return ''
  if (status === 'pending') return ' warning'
  if (status === 'failed' || status === 'rejected') return ' danger'
  return ' muted'
}

/** Story 6.6: assistant actions waiting for approval, and the user's own requests. */
export function AgentApprovalsSection({ initialScope = 'queue' }: { initialScope?: 'queue' | 'mine' }) {
  const [scope, setScope] = useState<'queue' | 'mine'>(initialScope)
  const [filter, setFilter] = useState<AgentApprovalStatus | ''>('pending')
  const [items, setItems] = useState<AgentActionApproval[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, { tone: 'success' | 'error'; message: string }>>({})

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const fetched = await getAgentApprovals(scope, filter)
      // Keep items decided on this page visible (with their feedback) after they leave the list.
      setItems((current) => [
        ...fetched,
        ...current.filter((item) => item.id in feedback && !fetched.some((f) => f.id === item.id)),
      ])
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Assistant approvals could not be loaded.')
    } finally {
      if (!quiet) setLoading(false)
    }
    // feedback is only read to keep decided items; it does not trigger a reload.
  }, [scope, filter])

  useEffect(() => {
    void load()
  }, [load])

  usePolling(() => load(true), POLL_MS, !loading)

  function replace(updated: AgentActionApproval) {
    setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)))
  }

  async function decide(item: AgentActionApproval, decision: 'approve' | 'reject') {
    setBusy(item.id)
    try {
      const result = await decideAgentApproval(item.id, decision, notes[item.id] ?? '')
      replace(result.approval)
      const failed = result.approval.status === 'failed'
      setFeedback((current) => ({
        ...current,
        [item.id]: { tone: failed ? 'error' : 'success', message: result.message },
      }))
    } catch (reason) {
      setFeedback((current) => ({
        ...current,
        [item.id]: { tone: 'error', message: reason instanceof Error ? reason.message : 'The decision could not be saved.' },
      }))
    } finally {
      setBusy(null)
    }
  }

  async function cancel(item: AgentActionApproval) {
    setBusy(item.id)
    try {
      replace(await cancelAgentApproval(item.id))
      setFeedback((current) => ({ ...current, [item.id]: { tone: 'success', message: 'Cancelled. Nothing was run.' } }))
    } catch (reason) {
      setFeedback((current) => ({
        ...current,
        [item.id]: { tone: 'error', message: reason instanceof Error ? reason.message : 'The request could not be cancelled.' },
      }))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="page-stack">
      <section className="task-workspace-card" aria-label="Assistant action approvals">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><Bot size={21} /></div>
          <div>
            <p className="eyebrow">Approvals</p>
            <h2>AI assistant actions</h2>
            <p>
              Medium-risk assistant actions wait for the requester to confirm; high-risk ones need
              HR / Operations, an admin, or the requester&apos;s team manager. Approved actions run
              with the requester&apos;s own permissions.
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          {initialScope !== 'mine' && (
            <select
              aria-label="Assistant approvals view"
              value={scope}
              onChange={(event) => setScope(event.target.value as 'queue' | 'mine')}
            >
              <option value="queue">Waiting for my approval</option>
              <option value="mine">My requests</option>
            </select>
          )}
          <select
            aria-label="Assistant approval status"
            value={filter}
            onChange={(event) => setFilter(event.target.value as AgentApprovalStatus | '')}
          >
            <option value="pending">Pending</option>
            <option value="">All</option>
          </select>
          <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error ? (
        <section className="meetings-error">
          <div>
            <h3>Assistant approvals unavailable</h3>
            <p>{error}</p>
          </div>
        </section>
      ) : loading ? (
        <div className="meetings-loading" role="status">Loading assistant approvals...</div>
      ) : items.length === 0 ? (
        <section className="meetings-empty">
          <div className="state-icon" aria-hidden="true"><Bot size={28} /></div>
          <h3>No {filter ? 'pending ' : ''}assistant actions</h3>
          <p>
            {scope === 'queue'
              ? 'High-risk assistant actions you can approve will appear here.'
              : 'Assistant actions you asked for that need approval will appear here.'}
          </p>
        </section>
      ) : (
        <ul className="approval-list">
          {items.map((item) => (
            <li key={item.id} className="approval-card" data-testid={`agent-approval-${item.id}`}>
              <div className="approval-card-header">
                <strong>{item.summary}</strong>
                <span className={`status-pill${statusTone(item.status)}`}>{STATUS_LABELS[item.status]}</span>
              </div>
              <p className="task-manage-hint">
                <span className={`agent-risk-badge ${item.risk_level}`}>{item.risk_level} risk</span>{' '}
                Requested by {item.requester_name} ({item.requester_role}
                {item.requester_team ? `, ${item.requester_team}` : ''}) on {formatTimestamp(item.created_at)}
                {item.status === 'pending' ? ` · expires ${formatTimestamp(item.expires_at)}` : ''}
              </p>
              {item.risk_reasons.length > 0 && (
                <ul className="agent-risk-reasons" aria-label="Why approval is required">
                  {item.risk_reasons.map((reason) => <li key={reason}>{reason}</li>)}
                </ul>
              )}
              {item.decided_by_name && (
                <p className="task-manage-hint">
                  Decided by {item.decided_by_name}
                  {item.decided_at ? ` on ${formatTimestamp(item.decided_at)}` : ''}
                  {item.decision_note ? `: ${item.decision_note}` : ''}
                </p>
              )}
              {item.error && <p className="task-manage-feedback error">{item.error}</p>}
              {feedback[item.id] && (
                <p
                  className={`task-manage-feedback ${feedback[item.id].tone}`}
                  role={feedback[item.id].tone === 'error' ? 'alert' : 'status'}
                >
                  {feedback[item.id].message}
                </p>
              )}
              {item.can_decide && (
                <div className="approval-actions">
                  {item.approver_kind === 'approver' && (
                    <input
                      type="text"
                      aria-label={`Decision note for ${item.summary}`}
                      placeholder="Note (optional)"
                      value={notes[item.id] ?? ''}
                      onChange={(event) => setNotes((current) => ({ ...current, [item.id]: event.target.value }))}
                      disabled={busy === item.id}
                    />
                  )}
                  <button className="button primary" type="button" onClick={() => void decide(item, 'approve')} disabled={busy === item.id}>
                    {item.approver_kind === 'requester' ? 'Confirm and run' : 'Approve'}
                  </button>
                  <button className="button secondary" type="button" onClick={() => void decide(item, 'reject')} disabled={busy === item.id}>
                    Reject
                  </button>
                </div>
              )}
              {item.can_cancel && !item.can_decide && (
                <div className="approval-actions">
                  <p className="task-manage-pending">Waiting for HR / Operations, an admin, or your manager.</p>
                  <button className="button secondary" type="button" onClick={() => void cancel(item)} disabled={busy === item.id}>
                    Cancel request
                  </button>
                </div>
              )}
              {item.can_cancel && item.can_decide && (
                <button className="button secondary" type="button" onClick={() => void cancel(item)} disabled={busy === item.id}>
                  Cancel request
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
