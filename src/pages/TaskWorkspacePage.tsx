import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { CalendarDays, CheckSquare, RefreshCw } from 'lucide-react'
import {
  getDashboardTasks,
  getMyTasks,
  retryAssignmentEmail,
  taskCalendarUrl,
  type TaskRecord,
} from '../api/tasks'
import { useAuth } from '../contexts/AuthContext'

type TaskWorkspaceView = 'mine' | 'dashboard' | 'calendar'

interface TaskWorkspacePageProps {
  view: TaskWorkspaceView
}

const DASHBOARD_REFRESH_INTERVAL_MS = 15_000

function formatSourceTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds - minutes * 60
  return `${minutes}:${remainingSeconds.toFixed(2).padStart(5, '0')}`
}

export function TaskWorkspacePage({ view }: TaskWorkspacePageProps) {
  const { user } = useAuth()
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [retryingTaskId, setRetryingTaskId] = useState<string | null>(null)
  const taskRequestRef = useRef<Promise<TaskRecord[]> | null>(null)

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
                  ? 'Deadlines from your assigned tasks, ready to add to a calendar.'
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
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id}>
                  <td>
                    <strong>{task.title}</strong>
                  </td>
                  {isDashboard && <td>{task.assignee_name}</td>}
                  <td>
                    <time dateTime={task.due_at}>
                      {new Intl.DateTimeFormat(undefined, {
                        dateStyle: 'medium',
                        timeStyle: 'short',
                      }).format(new Date(task.due_at))}
                    </time>
                  </td>
                  <td className="task-capitalize">{task.priority}</td>
                  <td><span className="status-pill muted">Pending</span></td>
                  <td>
                    <Link
                      to={`/meetings/${encodeURIComponent(task.meeting_id)}/report?transcript_id=${encodeURIComponent(task.transcript_id)}&segment=${task.source_segment_index}`}
                    >
                      {task.meeting_title} - {formatSourceTimestamp(task.source_timestamp_seconds)}
                    </Link>
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
