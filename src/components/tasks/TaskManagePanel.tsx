import { useCallback, useEffect, useMemo, useState } from 'react'
import { CalendarClock, History, UserRoundCog } from 'lucide-react'
import {
  changeTaskDueDate,
  getTaskAudit,
  getTaskNotifications,
  reassignTask,
  type TaskActor,
  type TaskAuditEntry,
  type TaskChangeResult,
  type TaskNotification,
  type TaskRecord,
} from '../../api/tasks'
import type { Employee } from '../../types/meeting'

interface TaskManagePanelProps {
  task: TaskRecord
  actor: TaskActor
  employees: Employee[]
  onTaskChanged: (task: TaskRecord) => void
}

type Feedback = { tone: 'success' | 'pending' | 'error'; message: string }

const OUTCOME_LABELS: Record<TaskAuditEntry['outcome'], string> = {
  applied: 'Applied',
  pending_approval: 'Awaiting approval',
  denied: 'Denied',
  rejected: 'Rejected',
}

const ACTION_LABELS: Record<string, string> = {
  reassign: 'Reassignment',
  change_due_date: 'Due date change',
  decide_reassignment: 'Approval decision',
}

const NOTIFICATION_LABELS: Record<string, string> = {
  reassigned_away: 'Removed from task',
  reassigned_to: 'Assigned task',
  due_date_changed: 'Due date changed',
}

/** Convert an ISO timestamp to the local "YYYY-MM-DDTHH:mm" a datetime-local input expects. */
function toLocalInputValue(iso: string): string {
  const date = new Date(iso)
  const offsetMs = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16)
}

function sameTeam(left: string | null | undefined, right: string | null | undefined): boolean {
  return Boolean(left && right && left.toLowerCase() === right.toLowerCase())
}

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(iso))
}

export function TaskManagePanel({ task, actor, employees, onTaskChanged }: TaskManagePanelProps) {
  const [assigneeId, setAssigneeId] = useState('')
  const [reassignReason, setReassignReason] = useState('')
  const [dueAt, setDueAt] = useState(() => toLocalInputValue(task.due_at))
  const [dueReason, setDueReason] = useState('')
  const [busy, setBusy] = useState<'reassign' | 'due' | null>(null)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const [audit, setAudit] = useState<TaskAuditEntry[]>([])
  const [notifications, setNotifications] = useState<TaskNotification[]>([])
  const [historyError, setHistoryError] = useState('')

  const isManager = actor.role === 'manager'
  const pending = task.pending_reassignment ?? null

  const loadHistory = useCallback(async () => {
    try {
      const [auditEntries, notificationEntries] = await Promise.all([
        getTaskAudit(task.id),
        getTaskNotifications(task.id),
      ])
      setAudit(auditEntries)
      setNotifications(notificationEntries)
      setHistoryError('')
    } catch (reason) {
      setHistoryError(reason instanceof Error ? reason.message : 'History could not be loaded.')
    }
  }, [task.id])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  useEffect(() => {
    setDueAt(toLocalInputValue(task.due_at))
  }, [task.due_at])

  const { teamMates, otherEmployees } = useMemo(() => {
    const candidates = employees.filter((employee) => employee.id !== task.assignee_id)
    return {
      teamMates: candidates.filter((employee) => sameTeam(employee.team, actor.team)),
      otherEmployees: candidates.filter((employee) => !sameTeam(employee.team, actor.team)),
    }
  }, [actor.team, employees, task.assignee_id])

  const selectedEmployee = employees.find((employee) => employee.id === assigneeId)
  const needsApproval = Boolean(
    isManager && selectedEmployee && !sameTeam(selectedEmployee.team, actor.team),
  )

  function handleResult(result: TaskChangeResult) {
    onTaskChanged(result.task)
    const failed = result.notifications.filter((notification) => notification.status === 'failed')
    setFeedback({
      tone: result.outcome === 'pending_approval' ? 'pending' : 'success',
      message: failed.length
        ? `${result.message} (Notification e-mail failed for ${failed.map((n) => n.recipient_name).join(', ')}.)`
        : result.message,
    })
    void loadHistory()
  }

  async function handleReassign() {
    if (!assigneeId) return
    setBusy('reassign')
    setFeedback(null)
    try {
      handleResult(await reassignTask(task.id, assigneeId, reassignReason, actor))
      setAssigneeId('')
      setReassignReason('')
    } catch (reason) {
      setFeedback({
        tone: 'error',
        message: reason instanceof Error ? reason.message : 'The task could not be reassigned.',
      })
      void loadHistory()
    } finally {
      setBusy(null)
    }
  }

  async function handleDueDate() {
    if (!dueAt) return
    setBusy('due')
    setFeedback(null)
    try {
      handleResult(
        await changeTaskDueDate(task.id, new Date(dueAt).toISOString(), dueReason, actor),
      )
      setDueReason('')
    } catch (reason) {
      setFeedback({
        tone: 'error',
        message: reason instanceof Error ? reason.message : 'The due date could not be changed.',
      })
      void loadHistory()
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="task-manage-panel" aria-label={`Manage ${task.title}`}>
      {feedback && (
        <p
          className={`task-manage-feedback ${feedback.tone}`}
          role={feedback.tone === 'error' ? 'alert' : 'status'}
        >
          {feedback.message}
        </p>
      )}

      <div className="task-manage-grid">
        <section className="task-manage-section">
          <h4><UserRoundCog size={16} aria-hidden="true" /> Reassign task</h4>
          <p className="task-manage-hint">
            Currently assigned to <strong>{task.assignee_name}</strong>
            {task.assignee_team ? ` (${task.assignee_team})` : ''}.
            {isManager && ` Reassigning outside ${actor.team ?? 'your team'} needs HR / Operations approval.`}
          </p>
          {pending ? (
            <p className="task-manage-pending" role="status">
              Awaiting approval to move this task to <strong>{pending.to_assignee_name}</strong>
              {pending.to_assignee_team ? ` (${pending.to_assignee_team})` : ''}.
            </p>
          ) : (
            <>
              <label htmlFor={`reassign-${task.id}`}>New assignee</label>
              <select
                id={`reassign-${task.id}`}
                value={assigneeId}
                onChange={(event) => setAssigneeId(event.target.value)}
                disabled={busy !== null}
              >
                <option value="">Select an employee</option>
                {teamMates.length > 0 && (
                  <optgroup label={`Your team (${actor.team ?? 'unassigned'})`}>
                    {teamMates.map((employee) => (
                      <option key={employee.id} value={employee.id}>{employee.name}</option>
                    ))}
                  </optgroup>
                )}
                {otherEmployees.length > 0 && (
                  <optgroup label={isManager ? 'Other teams (requires approval)' : 'Other teams'}>
                    {otherEmployees.map((employee) => (
                      <option key={employee.id} value={employee.id}>
                        {employee.name}{employee.team ? ` (${employee.team})` : ''}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
              <label htmlFor={`reassign-reason-${task.id}`}>Reason (optional)</label>
              <input
                id={`reassign-reason-${task.id}`}
                type="text"
                value={reassignReason}
                onChange={(event) => setReassignReason(event.target.value)}
                placeholder="e.g. Rahul is on leave this week"
                disabled={busy !== null}
              />
              {needsApproval && (
                <p className="task-manage-pending">
                  {selectedEmployee?.name} is outside your team. This will be sent for approval.
                </p>
              )}
              <button
                className="button primary"
                type="button"
                onClick={() => void handleReassign()}
                disabled={!assigneeId || busy !== null}
              >
                {busy === 'reassign'
                  ? 'Saving…'
                  : needsApproval ? 'Request approval' : 'Reassign'}
              </button>
            </>
          )}
        </section>

        <section className="task-manage-section">
          <h4><CalendarClock size={16} aria-hidden="true" /> Change due date</h4>
          <p className="task-manage-hint">
            The assignee is notified and their calendar entry is updated.
          </p>
          <label htmlFor={`due-${task.id}`}>New due date</label>
          <input
            id={`due-${task.id}`}
            type="datetime-local"
            value={dueAt}
            onChange={(event) => setDueAt(event.target.value)}
            disabled={busy !== null}
          />
          <label htmlFor={`due-reason-${task.id}`}>Reason (optional)</label>
          <input
            id={`due-reason-${task.id}`}
            type="text"
            value={dueReason}
            onChange={(event) => setDueReason(event.target.value)}
            placeholder="e.g. Client moved the review"
            disabled={busy !== null}
          />
          <button
            className="button primary"
            type="button"
            onClick={() => void handleDueDate()}
            disabled={!dueAt || busy !== null || dueAt === toLocalInputValue(task.due_at)}
          >
            {busy === 'due' ? 'Saving…' : 'Update due date'}
          </button>
        </section>
      </div>

      <section className="task-manage-section task-manage-history">
        <h4><History size={16} aria-hidden="true" /> History</h4>
        {historyError && <p role="alert">{historyError}</p>}
        {audit.length === 0 && notifications.length === 0 ? (
          <p className="task-manage-hint">No reassignments or due-date changes yet.</p>
        ) : (
          <ul>
            {audit.map((entry) => (
              <li key={entry.id}>
                <span className={`status-pill ${entry.outcome === 'applied' ? '' : entry.outcome === 'pending_approval' ? 'warning' : 'danger'}`}>
                  {OUTCOME_LABELS[entry.outcome]}
                </span>
                <span>
                  <strong>{ACTION_LABELS[entry.action] ?? entry.action}</strong> by {entry.actor_id} ({entry.actor_role})
                  {' · '}{formatTimestamp(entry.created_at)}
                  <small>{entry.detail}</small>
                </span>
              </li>
            ))}
            {notifications.map((notification) => (
              <li key={notification.id}>
                <span className={`status-pill ${notification.status === 'sent' ? '' : 'warning'}`}>
                  E-mail {notification.status}
                </span>
                <span>
                  <strong>{NOTIFICATION_LABELS[notification.kind] ?? notification.kind}</strong> → {notification.recipient_name}
                  {' · '}{formatTimestamp(notification.created_at)}
                  {notification.error && <small>{notification.error}</small>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
