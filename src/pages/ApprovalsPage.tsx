import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, ShieldCheck } from 'lucide-react'
import {
  decideReassignmentRequest,
  getReassignmentRequests,
  type TaskReassignmentRequest,
} from '../api/tasks'
import { useAuth } from '../contexts/AuthContext'

type StatusFilter = 'pending' | ''

const APPROVER_ROLES = new Set(['hr_operations', 'admin'])

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(iso))
}

export function ApprovalsPage() {
  const { user } = useAuth()
  const [filter, setFilter] = useState<StatusFilter>('pending')
  const [requests, setRequests] = useState<TaskReassignmentRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [deciding, setDeciding] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, { tone: 'success' | 'error'; message: string }>>({})

  const canDecide = Boolean(user && APPROVER_ROLES.has(user.role))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRequests(await getReassignmentRequests(filter))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Approval requests could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    void load()
  }, [load])

  async function decide(request: TaskReassignmentRequest, decision: 'approve' | 'reject') {
    if (!user) return
    setDeciding(request.id)
    try {
      const result = await decideReassignmentRequest(
        request.id,
        decision,
        notes[request.id] ?? '',
        { id: user.id, role: user.role, team: user.department || null },
      )
      setFeedback((current) => ({ ...current, [request.id]: { tone: 'success', message: result.message } }))
      if (result.reassignment_request) {
        const updated = result.reassignment_request
        setRequests((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      }
    } catch (reason) {
      setFeedback((current) => ({
        ...current,
        [request.id]: {
          tone: 'error',
          message: reason instanceof Error ? reason.message : 'The decision could not be saved.',
        },
      }))
    } finally {
      setDeciding(null)
    }
  }

  return (
    <div className="page-stack">
      <section className="task-workspace-card">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><ShieldCheck size={21} /></div>
          <div>
            <p className="eyebrow">Approvals</p>
            <h2>Task reassignment approvals</h2>
            <p>
              {canDecide
                ? 'Managers moving a task outside their team need your approval.'
                : 'Cross-team reassignments you request wait here for HR / Operations approval.'}
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <select
            aria-label="Request status"
            value={filter}
            onChange={(event) => setFilter(event.target.value as StatusFilter)}
          >
            <option value="pending">Pending</option>
            <option value="">All requests</option>
          </select>
          <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error && (
        <section className="meetings-error" role="alert">
          <div>
            <h3>Approvals unavailable</h3>
            <p>{error}</p>
          </div>
        </section>
      )}

      {loading ? (
        <div className="meetings-loading" role="status">Loading approval requests...</div>
      ) : requests.length === 0 ? (
        <section className="meetings-empty">
          <div className="state-icon" aria-hidden="true"><ShieldCheck size={28} /></div>
          <h3>No {filter === 'pending' ? 'pending ' : ''}requests</h3>
          <p>Cross-team task reassignments that need approval will appear here.</p>
        </section>
      ) : (
        <ul className="approval-list">
          {requests.map((request) => (
            <li key={request.id} className="approval-card">
              <div className="approval-card-header">
                <strong>{request.task_title}</strong>
                <span className={`status-pill ${request.status === 'pending' ? 'warning' : request.status === 'approved' ? '' : 'danger'}`}>
                  {request.status === 'pending' ? 'Pending' : request.status === 'approved' ? 'Approved' : 'Rejected'}
                </span>
              </div>
              <p>
                Move from <strong>{request.from_assignee_name}</strong> to{' '}
                <strong>{request.to_assignee_name}</strong>
                {request.to_assignee_team ? ` (${request.to_assignee_team})` : ''}
              </p>
              <p className="task-manage-hint">
                Requested by {request.requested_by} ({request.requester_role}
                {request.requester_team ? `, ${request.requester_team}` : ''}) on {formatTimestamp(request.created_at)}
                {request.reason ? ` · Reason: ${request.reason}` : ''}
              </p>
              {request.status !== 'pending' && request.decided_by && (
                <p className="task-manage-hint">
                  {request.status === 'approved' ? 'Approved' : 'Rejected'} by {request.decided_by}
                  {request.decided_at ? ` on ${formatTimestamp(request.decided_at)}` : ''}
                  {request.decision_note ? ` · ${request.decision_note}` : ''}
                </p>
              )}
              {feedback[request.id] && (
                <p
                  className={`task-manage-feedback ${feedback[request.id].tone}`}
                  role={feedback[request.id].tone === 'error' ? 'alert' : 'status'}
                >
                  {feedback[request.id].message}
                </p>
              )}
              {request.status === 'pending' && canDecide && (
                <div className="approval-actions">
                  <input
                    type="text"
                    aria-label={`Decision note for ${request.task_title}`}
                    placeholder="Note (optional)"
                    value={notes[request.id] ?? ''}
                    onChange={(event) => setNotes((current) => ({ ...current, [request.id]: event.target.value }))}
                    disabled={deciding === request.id}
                  />
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => void decide(request, 'approve')}
                    disabled={deciding === request.id}
                  >
                    Approve
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => void decide(request, 'reject')}
                    disabled={deciding === request.id}
                  >
                    Reject
                  </button>
                </div>
              )}
              {request.status === 'pending' && !canDecide && (
                <p className="task-manage-pending">Awaiting HR / Operations or Admin approval.</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
