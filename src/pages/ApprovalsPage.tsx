import { useCallback, useEffect, useMemo, useState } from 'react'
import { Calendar, RefreshCw, ShieldCheck } from 'lucide-react'
import {
  decideReassignmentRequest,
  getReassignmentRequests,
  type TaskReassignmentRequest,
} from '../api/tasks'
import {
  getRescheduleRequests,
  decideRescheduleRequest,
} from '../api/meetings'
import { AgentApprovalsSection } from '../components/assistant/AgentApprovalsSection'
import { AssistantApprovalHistory } from '../components/assistant/AssistantApprovalHistory'
import { EmailApprovalsSection } from '../components/email/EmailApprovalsSection'
import { useAuth } from '../contexts/AuthContext'
import type { MeetingRescheduleRequest } from '../types/meeting'

type StatusFilter = 'pending' | ''

const APPROVER_ROLES = new Set(['hr_operations', 'admin'])

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(iso))
}

function formatDateTimeRange(start: string, end: string): string {
  try {
    const toUTC = (s: string) =>
      s.endsWith('Z') || s.includes('+') || /[+-]\d{2}:\d{2}$/.test(s) ? new Date(s) : new Date(s + 'Z')
    const s = toUTC(start)
    const e = toUTC(end)
    if (isNaN(s.getTime()) || isNaN(e.getTime())) return `${start} – ${end}`
    const opts: Intl.DateTimeFormatOptions = {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    }
    return `${s.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}, ` +
      `${s.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })} – ` +
      `${e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
  } catch {
    return `${start} – ${end}`
  }
}

// ── Meeting Reschedule Requests Section ──────────────────────────────────────

interface RescheduleApprovalsSectionProps {
  actor: { id: string }
}

function MeetingRescheduleApprovalsSection({ actor }: RescheduleApprovalsSectionProps) {
  const [rescheduleFilter, setRescheduleFilter] = useState<StatusFilter>('pending')
  const [rescheduleRequests, setRescheduleRequests] = useState<MeetingRescheduleRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [deciding, setDeciding] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, { tone: 'success' | 'error'; message: string }>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRescheduleRequests(await getRescheduleRequests(actor, rescheduleFilter))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Reschedule requests could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [actor, rescheduleFilter])

  useEffect(() => {
    void load()
  }, [load])

  async function decide(request: MeetingRescheduleRequest, decision: 'approve' | 'reject') {
    setDeciding(request.id)
    try {
      const result = await decideRescheduleRequest(
        request.id,
        decision,
        notes[request.id] ?? '',
        actor,
      )
      setFeedback((current) => ({
        ...current,
        [request.id]: { tone: 'success', message: result.message },
      }))
      const updated = result.request
      setRescheduleRequests((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      )
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
    <section className="task-workspace-card" aria-label="Meeting reschedule approvals">
      <div className="task-workspace-heading">
        <div className="meeting-icon" aria-hidden="true"><Calendar size={21} /></div>
        <div>
          <p className="eyebrow">Meeting Reschedule Requests</p>
          <h2>Meeting rescheduling approvals</h2>
          <p>Pending reschedule requests for meetings you organise.</p>
        </div>
      </div>
      <div className="task-workspace-actions">
        <select
          aria-label="Reschedule request status filter"
          value={rescheduleFilter}
          onChange={(event) => setRescheduleFilter(event.target.value as StatusFilter)}
        >
          <option value="pending">Pending</option>
          <option value="">All requests</option>
        </select>
        <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={16} aria-hidden="true" /> Refresh
        </button>
      </div>

      {error && (
        <div className="meetings-error" role="alert">
          <p>{error}</p>
        </div>
      )}

      {loading ? (
        <div className="meetings-loading" role="status">Loading reschedule requests…</div>
      ) : rescheduleRequests.length === 0 ? (
        <div className="meetings-empty">
          <div className="state-icon" aria-hidden="true"><Calendar size={28} /></div>
          <h3>No {rescheduleFilter === 'pending' ? 'pending ' : ''}reschedule requests</h3>
          <p>Meeting reschedule requests that need your approval will appear here.</p>
        </div>
      ) : (
        <ul className="approval-list">
          {rescheduleRequests.map((request) => (
            <li key={request.id} className="approval-card">
              <div className="approval-card-header">
                <strong>{request.meeting_title}</strong>
                <span
                  className={`status-pill ${
                    request.status === 'pending'
                      ? 'warning'
                      : request.status === 'approved'
                        ? ''
                        : 'danger'
                  }`}
                >
                  {request.status === 'pending'
                    ? 'Pending'
                    : request.status === 'approved'
                      ? 'Approved'
                      : 'Rejected'}
                </span>
              </div>
              <p>
                Requested by{' '}
                <strong>{request.requested_by_name ?? request.requested_by}</strong>
                {request.attendee_count > 0 ? ` · ${request.attendee_count} attendee${request.attendee_count === 1 ? '' : 's'}` : ''}
              </p>
              <p className="task-manage-hint">
                New time: {formatDateTimeRange(request.new_start_at, request.new_end_at)}
              </p>
              <p className="task-manage-hint">
                Requested on {formatTimestamp(request.created_at)}
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
              {request.status === 'pending' && (
                <div className="approval-actions">
                  <input
                    type="text"
                    aria-label={`Decision note for reschedule of ${request.meeting_title}`}
                    placeholder="Note (optional)"
                    value={notes[request.id] ?? ''}
                    onChange={(event) =>
                      setNotes((current) => ({ ...current, [request.id]: event.target.value }))
                    }
                    disabled={deciding === request.id}
                  />
                  <button
                    id={`reschedule-approve-${request.id}`}
                    className="button primary"
                    type="button"
                    onClick={() => void decide(request, 'approve')}
                    disabled={deciding === request.id}
                  >
                    Approve
                  </button>
                  <button
                    id={`reschedule-reject-${request.id}`}
                    className="button secondary"
                    type="button"
                    onClick={() => void decide(request, 'reject')}
                    disabled={deciding === request.id}
                  >
                    Reject
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// ── Main Approvals Page ───────────────────────────────────────────────────────

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
  const canViewAssistantApprovalHistory = Boolean(
    user && APPROVER_ROLES.has(user.role),
  )
  const emailActor = useMemo(
    () => (user ? { id: user.id, role: user.role, team: user.department || null } : null),
    [user],
  )
  const meetingActor = useMemo(() => (user ? { id: user.id } : null), [user])

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

      {/* Meeting reschedule approvals \u2014 visible to all users; API filters by organiser */}
      {meetingActor && <MeetingRescheduleApprovalsSection actor={meetingActor} />}

      {emailActor && <EmailApprovalsSection actor={emailActor} />}

      {/* Story 6.6: assistant actions paused with "Approval Required" */}
      {user && (
        <AgentApprovalsSection
          canAssignAlternateApprover={canViewAssistantApprovalHistory}
        />
      )}
      <AssistantApprovalHistory visible={canViewAssistantApprovalHistory} />
    </div>
  )
}
