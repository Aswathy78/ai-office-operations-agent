import { useEffect, useState } from 'react'
import { History, RefreshCw } from 'lucide-react'
import {
  getAgentApprovalHistory,
  type AgentActionApproval,
  type AgentApprovalStatus,
} from '../../api/assistantApprovals'

const HISTORY_STATUSES: AgentApprovalStatus[] = [
  'executed',
  'rejected',
  'failed',
  'cancelled',
  'expired',
]

function formatTimestamp(iso: string | null): string {
  if (!iso) return '—'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(iso))
}

export function AssistantApprovalHistory({ visible }: { visible: boolean }) {
  const [status, setStatus] = useState<AgentApprovalStatus | ''>('')
  const [approver, setApprover] = useState('')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [items, setItems] = useState<AgentActionApproval[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    setLoading(true)
    try {
      setItems(await getAgentApprovalHistory({
        status,
        approver,
        from_date: fromDate,
        to_date: toDate,
      }))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Approval history could not be loaded.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (visible) void load()
  // Load on initial authorized render; filters are applied by the form.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible])

  if (!visible) return null

  return (
    <section className="task-workspace-card" aria-label="Assistant approval history">
      <div className="task-workspace-heading">
        <div className="meeting-icon" aria-hidden="true"><History size={21} /></div>
        <div>
          <p className="eyebrow">Audit</p>
          <h2>AI action approval history</h2>
          <p>Decision status, approver identity, and timestamp for assistant action requests.</p>
        </div>
      </div>
      <form
        className="task-workspace-actions"
        onSubmit={(event) => {
          event.preventDefault()
          void load()
        }}
      >
        <select
          aria-label="Approval history status"
          value={status}
          onChange={(event) => setStatus(event.target.value as AgentApprovalStatus | '')}
        >
          <option value="">All decisions</option>
          {HISTORY_STATUSES.map((value) => (
            <option key={value} value={value}>{value[0].toUpperCase() + value.slice(1)}</option>
          ))}
        </select>
        <input
          aria-label="Approval history approver"
          placeholder="Approver name or email"
          value={approver}
          onChange={(event) => setApprover(event.target.value)}
        />
        <label>
          From
          <input
            aria-label="Approval history from date"
            type="date"
            value={fromDate}
            onChange={(event) => setFromDate(event.target.value)}
          />
        </label>
        <label>
          To
          <input
            aria-label="Approval history to date"
            type="date"
            value={toDate}
            onChange={(event) => setToDate(event.target.value)}
          />
        </label>
        <button className="button secondary" type="submit" disabled={loading}>
          <RefreshCw size={16} aria-hidden="true" /> Filter history
        </button>
      </form>

      {error && <p className="task-manage-feedback error" role="alert">{error}</p>}
      {loading ? (
        <div className="meetings-loading" role="status">Loading approval history…</div>
      ) : items.length === 0 ? (
        <div className="meetings-empty">
          <h3>No matching approval decisions</h3>
        </div>
      ) : (
        <ul className="approval-list">
          {items.map((item) => (
            <li key={item.id} className="approval-card">
              <div className="approval-card-header">
                <strong>{item.summary}</strong>
                <span className="status-pill">{item.status}</span>
              </div>
              <p className="task-manage-hint">
                Requested by {item.requester_name} on {formatTimestamp(item.created_at)}
              </p>
              <p className="task-manage-hint">
                {item.history_actor_name ?? item.decided_by_name ?? item.decided_by ?? 'System'}
                {' · '}{formatTimestamp(item.history_at ?? item.decided_at)}
                {item.decision_note ? ` · ${item.decision_note}` : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
