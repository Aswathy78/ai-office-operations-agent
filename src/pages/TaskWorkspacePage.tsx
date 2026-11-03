import { Fragment, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { CalendarDays, CheckSquare, RefreshCw } from 'lucide-react'
import { getActiveEmployees, getCalendarMeetings } from '../api/meetings'
import {
  getDashboardTasks,
  getMyTasks,
  retryAssignmentEmail,
  retryTaskCalendarSync,
  setTaskStatus,
  taskCalendarUrl,
  type TaskActor,
  type TaskRecord,
} from '../api/tasks'
import { EmailDeliveryPanel } from '../components/email/EmailDeliveryPanel'
import { EscalationPanel } from '../components/tasks/EscalationPanel'
import { CALENDAR_SYNC_IN_FLIGHT, CalendarSyncStatus } from '../components/tasks/CalendarSyncStatus'
import { TaskCalendarMonth } from '../components/tasks/TaskCalendarMonth'
import { TaskManagePanel } from '../components/tasks/TaskManagePanel'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'
import type { Employee } from '../types/meeting'
import type { MeetingCalendarEvent } from '../types/meeting'

type TaskWorkspaceView = 'mine' | 'dashboard' | 'calendar'

interface TaskWorkspacePageProps {
  view: TaskWorkspaceView
}

const DASHBOARD_REFRESH_INTERVAL_MS = 15_000
const CALENDAR_SYNC_POLL_MS = 3_000

function formatSourceTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds - minutes * 60
  return `${minutes}:${remainingSeconds.toFixed(2).padStart(5, '0')}`
}

export function TaskWorkspacePage({ view }: TaskWorkspacePageProps) {
  const [searchParams] = useSearchParams()
  const { user } = useAuth()
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [calendarMeetings, setCalendarMeetings] = useState<MeetingCalendarEvent[]>([])
  const [calendarTimeZone, setCalendarTimeZone] = useState('UTC')
  const [calendarLoading, setCalendarLoading] = useState(false)
  const [calendarError, setCalendarError] = useState('')
  const [calendarMonth, setCalendarMonth] = useState(() => {
    const now = new Date()
    return { year: now.getFullYear(), month: now.getMonth() + 1 }
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [retryingTaskId, setRetryingTaskId] = useState<string | null>(null)
  const [statusTaskId, setStatusTaskId] = useState<string | null>(null)
  const [calendarRetryTaskId, setCalendarRetryTaskId] = useState<string | null>(null)
  const [managingTaskId, setManagingTaskId] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [employees, setEmployees] = useState<Employee[]>([])
  const taskRequestRef = useRef<Promise<TaskRecord[]> | null>(null)
  const selectedTaskRowRef = useRef<HTMLTableRowElement | null>(null)

  const canManageTasks = view === 'dashboard' && user?.role !== 'employee'
  const actor: TaskActor | null = user
    ? { id: user.id, role: user.role, team: user.department || null, email: user.email }
    : null

  useEffect(() => {
    if (!canManageTasks) return
    let active = true
    getActiveEmployees()
      .then((result) => { if (active) setEmployees(result) })
      .catch(() => { if (active) setEmployees([]) })
    return () => { active = false }
  }, [canManageTasks])

  useEffect(() => {
    if (view !== 'calendar' || !user) return
    let active = true
    setCalendarLoading(true)
    setCalendarError('')
    setCalendarMeetings([])
    getCalendarMeetings(calendarMonth.year, calendarMonth.month).then((result) => {
      if (!active) return
      setCalendarMeetings(result.meetings)
      setCalendarTimeZone(result.timezone)
    }).catch((reason: unknown) => {
      if (!active) return
      setCalendarError(
        reason instanceof Error ? reason.message : 'Scheduled meetings could not be loaded.',
      )
    }).finally(() => {
      if (active) setCalendarLoading(false)
    })
    return () => { active = false }
  }, [calendarMonth, refreshKey, user, view])

  function replaceTask(updated: TaskRecord) {
    setTasks((current) => current.map((task) => (task.id === updated.id ? updated : task)))
  }

  useEffect(() => {
    let active = true
    if (!user) return
    const assigneeEmail = user.email
    let pollTimeout: number | undefined

    async function loadTasks(showLoading: boolean) {
      if (showLoading) {
        setLoading(true)
        setError('')
      }

      let request = taskRequestRef.current
      if (!request) {
        request = view === 'dashboard'
          ? getDashboardTasks()
          : getMyTasks(assigneeEmail)
        taskRequestRef.current = request
        void request.then(
          () => {
            if (taskRequestRef.current === request) taskRequestRef.current = null
          },
          () => {
            if (taskRequestRef.current === request) taskRequestRef.current = null
          },
        )
      }

      try {
        const result = await request
        if (active) {
          setTasks(result)
          setError('')
        }
      } catch (reason: unknown) {
        if (active) {
          setError(reason instanceof Error ? reason.message : 'Tasks could not be loaded.')
        }
      } finally {
        if (active && showLoading) setLoading(false)
      }
    }

    function scheduleDashboardRefresh() {
      pollTimeout = window.setTimeout(() => {
        if (!active) return
        if (document.visibilityState !== 'hidden') void loadTasks(false)
        scheduleDashboardRefresh()
      }, DASHBOARD_REFRESH_INTERVAL_MS)
    }

    void loadTasks(true)
    if (view === 'dashboard') scheduleDashboardRefresh()

    return () => {
      active = false
      if (pollTimeout !== undefined) window.clearTimeout(pollTimeout)
    }
  }, [refreshKey, user, view])

  useEffect(() => {
    const requestedTaskId = searchParams.get('task_id')
    if (!requestedTaskId) {
      setSelectedTaskId(null)
      return
    }
    if (loading) return
    setSelectedTaskId(
      tasks.some((task) => task.id === requestedTaskId) ? requestedTaskId : null,
    )
  }, [loading, searchParams, tasks])

  useEffect(() => {
    if (!selectedTaskId) return
    const row = selectedTaskRowRef.current
    if (!row) return
    row.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
    row.focus()
  }, [selectedTaskId])

  async function handleRetryEmail(taskId: string) {
    setRetryingTaskId(taskId)
    try {
      const updated = await retryAssignmentEmail(taskId)
      setTasks((current) =>
        current.map((task) => (task.id === updated.id ? updated : task)),
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Email could not be retried.')
    } finally {
      setRetryingTaskId(null)
    }
  }

  async function handleCalendarRetry(taskId: string) {
    setCalendarRetryTaskId(taskId)
    try {
      replaceTask(await retryTaskCalendarSync(taskId))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Calendar sync could not be retried.')
    } finally {
      setCalendarRetryTaskId(null)
    }
  }

  // While a deadline is being written to the calendar, refresh quietly so
  // Pending -> Processing -> Completed / Retrying / Failed shows up promptly.
  const calendarSyncInFlight = tasks.some(
    (task) => task.calendar_sync && CALENDAR_SYNC_IN_FLIGHT.has(task.calendar_sync.status),
  )
  usePolling(
    async () => {
      if (!user) return
      setTasks(view === 'dashboard' ? await getDashboardTasks() : await getMyTasks(user.email))
    },
    CALENDAR_SYNC_POLL_MS,
    calendarSyncInFlight && !loading && view !== 'calendar',
  )

  async function handleStatusChange(task: TaskRecord) {
    if (!actor) return
    setStatusTaskId(task.id)
    try {
      const result = await setTaskStatus(
        task.id,
        task.status === 'completed' ? 'pending' : 'completed',
        actor,
      )
      replaceTask(result.task)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Task status could not be updated.')
    } finally {
      setStatusTaskId(null)
    }
  }

  const isCalendar = view === 'calendar'
  const isDashboard = view === 'dashboard'
  const title = isDashboard ? 'Task Dashboard' : isCalendar ? 'Task Calendar' : 'My Tasks'

  return (
    <div className="page-stack">
      <section className="task-workspace-card">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true">
            {isCalendar ? <CalendarDays size={21} /> : <CheckSquare size={21} />}
          </div>
          <div>
            <p className="eyebrow">{isDashboard ? 'Manager workspace' : 'Task management'}</p>
            <h2>{title}</h2>
            <p>
              {isDashboard
                ? 'Tasks created from confirmed meeting action items.'
                : isCalendar
                  ? 'Your task deadlines and scheduled meetings in one calendar.'
                  : 'Tasks assigned to your employee account.'}
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          {isCalendar && user && (
            <a className="button secondary" href={taskCalendarUrl(user.email)}>
              <CalendarDays size={16} aria-hidden="true" />
              Add deadlines to calendar
            </a>
          )}
          <button
            className="button secondary"
            type="button"
            onClick={() => setRefreshKey((current) => current + 1)}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            Refresh
          </button>
        </div>
      </section>

      {isDashboard && canManageTasks && <EscalationPanel />}

      {isDashboard && canManageTasks && actor && <EmailDeliveryPanel actor={actor} />}

      {error && (
        <section className="meetings-error" role="alert">
          <div>
            <h3>Task data unavailable</h3>
            <p>{error}</p>
          </div>
        </section>
      )}

      {loading ? (
        <div className="meetings-loading" role="status">Loading tasks...</div>
      ) : isCalendar ? (
        <TaskCalendarMonth
          tasks={tasks}
          meetings={calendarMeetings}
          timeZone={calendarTimeZone}
          meetingsLoading={calendarLoading}
          meetingsError={calendarError}
          onVisibleMonthChange={(year, month) => {
            setCalendarMonth((current) =>
              current.year === year && current.month === month
                ? current
                : { year, month },
            )
          }}
        />
      ) : tasks.length === 0 ? (
        <section className="meetings-empty">
          <div className="state-icon" aria-hidden="true">
            {isCalendar ? <CalendarDays size={28} /> : <CheckSquare size={28} />}
          </div>
          <h3>No tasks yet</h3>
          <p>Confirmed and assigned meeting action items will appear here.</p>
        </section>
      ) : (
        <div className="task-table-wrap">
          <table className="task-table">
            <thead>
              <tr>
                <th scope="col">Task</th>
                {isDashboard && <th scope="col">Assignee</th>}
                <th scope="col">Due</th>
                <th scope="col">Priority</th>
                <th scope="col">Status</th>
                <th scope="col">Source</th>
                <th scope="col">Assignment email</th>
                <th scope="col">Calendar</th>
                {canManageTasks && <th scope="col"><span className="visually-hidden">Actions</span></th>}
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <Fragment key={task.id}>
                <tr
                  ref={task.id === selectedTaskId ? selectedTaskRowRef : null}
                  className={task.id === selectedTaskId ? 'task-row-selected' : undefined}
                  aria-selected={task.id === selectedTaskId}
                  tabIndex={task.id === selectedTaskId ? -1 : undefined}
                >
                  <td>
                    <strong>{task.title}</strong>
                  </td>
                  {isDashboard && (
                    <td>
                      {task.assignee_name}
                      {task.assignee_team && <small className="task-cell-note">{task.assignee_team}</small>}
                      {task.pending_reassignment && (
                        <span className="status-pill warning task-cell-note">
                          Awaiting approval → {task.pending_reassignment.to_assignee_name}
                        </span>
                      )}
                    </td>
                  )}
                  <td>
                    <time dateTime={task.due_at}>
                      {new Intl.DateTimeFormat(undefined, {
                        dateStyle: 'medium',
                        timeStyle: 'short',
                      }).format(new Date(task.due_at))}
                    </time>
                  </td>
                  <td className="task-capitalize">{task.priority}</td>
                  <td>
                    <div className="task-status-cell">
                      {task.status === 'completed' ? (
                        <span className="status-pill">Completed</span>
                      ) : (
                        <span className="status-pill muted">Pending</span>
                      )}
                      {task.status !== 'completed' && new Date(task.due_at).getTime() < Date.now() && (
                        <span className="status-pill danger">Overdue</span>
                      )}
                      {(view === 'mine' || canManageTasks) && actor && (
                        <button
                          className="text-button"
                          type="button"
                          disabled={statusTaskId === task.id}
                          onClick={() => void handleStatusChange(task)}
                        >
                          {statusTaskId === task.id
                            ? 'Saving...'
                            : task.status === 'completed' ? 'Reopen' : 'Mark complete'}
                        </button>
                      )}
                    </div>
                  </td>
                  <td>
                    {task.meeting_id && task.transcript_id ? (
                      <Link
                        to={`/meetings/${encodeURIComponent(task.meeting_id)}/report?transcript_id=${encodeURIComponent(task.transcript_id)}&segment=${task.source_segment_index}`}
                      >
                        {task.meeting_title} - {formatSourceTimestamp(task.source_timestamp_seconds ?? 0)}
                      </Link>
                    ) : (
                      <span>AI assistant</span>
                    )}
                  </td>
                  <td>
                    {task.assignment_email_status === 'sent' ? (
                      <span className="status-pill">Sent</span>
                    ) : (
                      <div className="task-email-state">
                        <span className="status-pill warning">
                          {task.assignment_email_status === 'failed' ? 'Failed' : 'Pending'}
                        </span>
                        {task.assignment_email_error && (
                          <small role="status">{task.assignment_email_error}</small>
                        )}
                        <button
                          className="text-button"
                          type="button"
                          disabled={retryingTaskId === task.id}
                          onClick={() => void handleRetryEmail(task.id)}
                        >
                          {retryingTaskId === task.id ? 'Retrying...' : 'Retry'}
                        </button>
                      </div>
                    )}
                  </td>
                  <td>
                    <CalendarSyncStatus
                      sync={task.calendar_sync}
                      canRetry={Boolean(actor)}
                      retrying={calendarRetryTaskId === task.id}
                      onRetry={() => void handleCalendarRetry(task.id)}
                    />
                  </td>
                  {canManageTasks && (
                    <td>
                      <button
                        className="button secondary task-manage-toggle"
                        type="button"
                        aria-expanded={managingTaskId === task.id}
                        onClick={() =>
                          setManagingTaskId((current) => (current === task.id ? null : task.id))
                        }
                      >
                        {managingTaskId === task.id ? 'Close' : 'Manage'}
                      </button>
                    </td>
                  )}
                </tr>
                {canManageTasks && actor && managingTaskId === task.id && (
                  <tr className="task-manage-row">
                    <td colSpan={9}>
                      <TaskManagePanel
                        task={task}
                        actor={actor}
                        employees={employees}
                        onTaskChanged={replaceTask}
                      />
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
