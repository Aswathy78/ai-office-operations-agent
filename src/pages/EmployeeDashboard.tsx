import { useCallback, useEffect, useMemo, useState } from 'react'
import { Bell, CalendarDays, CheckSquare, Clock3, RefreshCw, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  decideAgentApproval,
  getAgentApprovals,
  type AgentActionApproval,
} from '../api/assistantApprovals'
import { getMeetings } from '../api/meetings'
import { getNotifications, getNotificationPreferences, type NotificationPreferences, type UserNotification } from '../api/notifications'
import { getMyTasks, type TaskRecord } from '../api/tasks'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'
import type { MeetingListRecord } from '../types/meeting'

const DASHBOARD_REFRESH_INTERVAL_MS = 3000

type DashboardSection = 'meetings' | 'tasks' | 'notifications' | 'approvals' | 'timezone'
type DashboardErrors = Partial<Record<DashboardSection, string>>

function parseDate(value: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`)
}

function dateKeyInTimeZone(value: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(value)
  const fields = Object.fromEntries(parts.map(({ type, value: part }) => [type, part]))
  return `${fields.year}-${fields.month}-${fields.day}`
}

function weekDateRange(now: Date, timeZone: string): { start: string; end: string } {
  const today = dateKeyInTimeZone(now, timeZone)
  const utcDate = new Date(`${today}T00:00:00Z`)
  const daysSinceMonday = (utcDate.getUTCDay() + 6) % 7
  utcDate.setUTCDate(utcDate.getUTCDate() - daysSinceMonday)
  const start = utcDate.toISOString().slice(0, 10)
  utcDate.setUTCDate(utcDate.getUTCDate() + 6)
  return { start, end: utcDate.toISOString().slice(0, 10) }
}

function formatTime(value: string, timeZone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone,
  }).format(parseDate(value))
}

function taskStatusLabel(status: TaskRecord['status']): string {
  return status === 'completed' ? 'Completed' : 'Pending'
}

function taskEmailLabel(status: TaskRecord['assignment_email_status']): string {
  if (status === 'sent') return 'Sent'
  if (status === 'failed') return 'Failed'
  return 'Pending'
}

function meetingIsJoinableToday(meeting: MeetingListRecord, now: Date, timeZone: string): boolean {
  const status = meeting.status.toLowerCase()
  if (status === 'cancelled' || status === 'completed') return false
  const start = parseDate(meeting.start_at)
  const end = parseDate(meeting.end_at)
  const active = start.getTime() <= now.getTime() && now.getTime() <= end.getTime()
  return dateKeyInTimeZone(start, timeZone) === dateKeyInTimeZone(now, timeZone) || active
}

export function EmployeeDashboard() {
  const { user } = useAuth()
  const [meetings, setMeetings] = useState<MeetingListRecord[]>([])
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [notifications, setNotifications] = useState<UserNotification[]>([])
  const [approvals, setApprovals] = useState<AgentActionApproval[]>([])
  const [preferences, setPreferences] = useState<NotificationPreferences | null>(null)
  const [timezoneFallback, setTimezoneFallback] = useState(true)
  const [errors, setErrors] = useState<DashboardErrors>({})
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [busyApprovalId, setBusyApprovalId] = useState<string | null>(null)
  const [approvalError, setApprovalError] = useState('')

  const load = useCallback(async () => {
    if (!user) return
    const results = await Promise.allSettled([
      getMeetings(),
      getMyTasks(user.email),
      getNotifications(),
      getAgentApprovals('queue', 'pending'),
      getNotificationPreferences(),
    ] as const)
    const nextErrors: DashboardErrors = {}
    const [meetingResult, taskResult, notificationResult, approvalResult, preferenceResult] = results

    if (meetingResult.status === 'fulfilled') setMeetings(meetingResult.value)
    else nextErrors.meetings = meetingResult.reason instanceof Error ? meetingResult.reason.message : 'Meetings could not be loaded.'

    if (taskResult.status === 'fulfilled') setTasks(taskResult.value)
    else nextErrors.tasks = taskResult.reason instanceof Error ? taskResult.reason.message : 'Tasks could not be loaded.'

    if (notificationResult.status === 'fulfilled') setNotifications(notificationResult.value)
    else nextErrors.notifications = notificationResult.reason instanceof Error ? notificationResult.reason.message : 'Notifications could not be loaded.'

    if (approvalResult.status === 'fulfilled') setApprovals(approvalResult.value)
    else nextErrors.approvals = approvalResult.reason instanceof Error ? approvalResult.reason.message : 'Approvals could not be loaded.'

    if (preferenceResult.status === 'fulfilled') {
      setPreferences(preferenceResult.value)
      setTimezoneFallback(preferenceResult.value.timezone_fallback)
    } else {
      setTimezoneFallback(true)
      nextErrors.timezone = preferenceResult.reason instanceof Error ? preferenceResult.reason.message : 'Employee timezone could not be loaded.'
    }

    setErrors(nextErrors)
    setLoading(false)
  }, [user])

  useEffect(() => {
    void load()
  }, [load])
  usePolling(load, DASHBOARD_REFRESH_INTERVAL_MS, true)

  const timeZone = preferences?.timezone_name || 'UTC'
  const now = new Date()
  const todayKey = dateKeyInTimeZone(now, timeZone)
  const thisWeek = weekDateRange(now, timeZone)
  const todaysMeetings = useMemo(
    () => meetings.filter((meeting) => meetingIsJoinableToday(meeting, now, timeZone))
      .sort((left, right) => parseDate(left.start_at).getTime() - parseDate(right.start_at).getTime()),
    [meetings, todayKey, timeZone],
  )
  const tasksDueThisWeek = useMemo(
    () => tasks
      .filter((task) => {
        const dueDate = dateKeyInTimeZone(parseDate(task.due_at), timeZone)
        return dueDate >= thisWeek.start && dueDate <= thisWeek.end
      })
      .sort((left, right) => parseDate(left.due_at).getTime() - parseDate(right.due_at).getTime()),
    [tasks, thisWeek.start, thisWeek.end, timeZone],
  )
  const taskPreview = useMemo(
    () => [...tasks]
      .sort((left, right) => {
        if (left.status !== right.status) return left.status === 'pending' ? -1 : 1
        return parseDate(left.due_at).getTime() - parseDate(right.due_at).getTime()
      })
      .slice(0, 3),
    [tasks],
  )
  const pendingCount = tasks.filter((task) => task.status === 'pending').length
  const completedCount = tasks.filter((task) => task.status === 'completed').length

  async function refresh() {
    setRefreshing(true)
    try {
      await load()
    } finally {
      setRefreshing(false)
    }
  }

  async function decide(approval: AgentActionApproval, decision: 'approve' | 'reject') {
    setBusyApprovalId(approval.id)
    setApprovalError('')
    try {
      await decideAgentApproval(approval.id, decision, '')
      setApprovals((current) => current.filter((item) => item.id !== approval.id))
    } catch (reason) {
      setApprovalError(reason instanceof Error ? reason.message : 'The approval decision could not be saved.')
    } finally {
      setBusyApprovalId(null)
    }
  }

  return (
    <div className="page-stack employee-dashboard">
      <section className="hero-card" aria-labelledby="employee-dashboard-title">
        <div>
          <p className="eyebrow light">Employee workspace</p>
          <h2 id="employee-dashboard-title">Good morning, {user?.name}</h2>
          <p>Your meetings, tasks, notifications, and approvals in one place.</p>
        </div>
        <button className="button ghost-button" type="button" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw size={16} aria-hidden="true" />
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </section>

      <p className="employee-dashboard-timezone" role="note">
        Dates shown in {timeZone}.
        {timezoneFallback && ' Employee timezone is unavailable; using UTC.'}
        {errors.timezone && ` Timezone preference could not be loaded: ${errors.timezone}`}
      </p>

      <div className="employee-dashboard-grid">
        <section className="employee-dashboard-section" aria-labelledby="employee-meetings-title">
          <div className="section-heading">
            <div><p className="eyebrow">Schedule</p><h2 id="employee-meetings-title">Today’s meetings</h2></div>
            <CalendarDays size={18} aria-hidden="true" />
          </div>
          {errors.meetings && <p className="task-manage-feedback error" role="alert">{errors.meetings}</p>}
          {loading && meetings.length === 0 ? (
            <p className="meetings-loading" role="status">Loading meetings…</p>
          ) : todaysMeetings.length === 0 ? (
            <p className="employee-dashboard-empty">No meetings today.</p>
          ) : (
            <ul className="employee-dashboard-list">
              {todaysMeetings.map((meeting) => (
                <li className="employee-dashboard-item" key={meeting.id}>
                  <div>
                    <strong>{meeting.title}</strong>
                    <span>{formatTime(meeting.start_at, timeZone)} – {formatTime(meeting.end_at, timeZone)}</span>
                  </div>
                  {meeting.join_url && (
                    <Link
                      className="button secondary"
                      to={`/meetings/${encodeURIComponent(meeting.id)}`}
                      aria-label={`Join ${meeting.title}`}
                    >
                      Join
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          )}
          <Link className="text-link" to="/meetings">All meetings</Link>
        </section>

        <section className="employee-dashboard-section" aria-labelledby="employee-tasks-title">
          <div className="section-heading">
            <div><p className="eyebrow">Work</p><h2 id="employee-tasks-title">My tasks</h2></div>
            <CheckSquare size={18} aria-hidden="true" />
          </div>
          {errors.tasks && <p className="task-manage-feedback error" role="alert">{errors.tasks}</p>}
          {loading && tasks.length === 0 ? (
            <p className="meetings-loading" role="status">Loading tasks…</p>
          ) : (
            <>
              <div className="employee-dashboard-task-summary" aria-label="Task counts by status">
                <span><strong>{pendingCount}</strong> Pending</span>
                <span><strong>{completedCount}</strong> Completed</span>
              </div>
              {tasks.length === 0 ? (
                <p className="employee-dashboard-empty">No tasks assigned.</p>
              ) : (
                <ul className="employee-dashboard-list">
                  {taskPreview.map((task) => (
                    <li className="employee-dashboard-item" key={task.id}>
                      <div>
                        <strong>{task.title}</strong>
                        <span>Task assignment email: {taskEmailLabel(task.assignment_email_status)}</span>
                      </div>
                      <span className={`status-pill${task.status === 'pending' ? ' warning' : ' muted'}`}>
                        {taskStatusLabel(task.status)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
          <Link className="text-link employee-dashboard-view-all" to="/tasks">View all tasks</Link>
        </section>

        <section className="employee-dashboard-section" aria-labelledby="employee-deadlines-title">
          <div className="section-heading">
            <div><p className="eyebrow">Deadlines</p><h2 id="employee-deadlines-title">Due this week</h2></div>
            <Clock3 size={18} aria-hidden="true" />
          </div>
          {errors.tasks && <p className="task-manage-feedback error" role="alert">{errors.tasks}</p>}
          {!loading && tasksDueThisWeek.length === 0 ? (
            <p className="employee-dashboard-empty">No tasks due this week.</p>
          ) : (
            <ul className="employee-dashboard-list">
              {tasksDueThisWeek.slice(0, 3).map((task) => (
                <li className="employee-dashboard-item" key={task.id}>
                  <div>
                    <strong>{task.title}</strong>
                    <span>{formatTime(task.due_at, timeZone)}</span>
                  </div>
                  <div className="employee-dashboard-task-status">
                    <span className={`status-pill${task.status === 'pending' ? ' warning' : ' muted'}`}>
                      {taskStatusLabel(task.status)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {tasksDueThisWeek.length > 3 && (
            <p className="employee-dashboard-more" aria-live="polite">
              Showing 3 of {tasksDueThisWeek.length} tasks due this week.
            </p>
          )}
          <Link className="text-link employee-dashboard-view-all" to="/tasks">View all deadlines</Link>
        </section>

        <section className="employee-dashboard-section" aria-labelledby="employee-notifications-title">
          <div className="section-heading">
            <div><p className="eyebrow">Inbox</p><h2 id="employee-notifications-title">Notifications</h2></div>
            <Bell size={18} aria-hidden="true" />
          </div>
          {errors.notifications && <p className="task-manage-feedback error" role="alert">{errors.notifications}</p>}
          {!loading && notifications.length === 0 ? (
            <p className="employee-dashboard-empty">You’re all caught up.</p>
          ) : (
            <ul className="employee-dashboard-list">
              {notifications.slice(0, 5).map((notification) => (
                <li className="employee-dashboard-item" key={notification.id}>
                  <div>
                    <strong>{notification.summary}</strong>
                    <span>{formatTime(notification.created_at, timeZone)}</span>
                  </div>
                  <span className={`status-pill${notification.read_at ? ' muted' : ' warning'}`}>
                    {notification.read_at ? 'Read' : 'Unread'}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <Link className="text-link employee-dashboard-view-all" to="/notifications">View all notifications</Link>
        </section>

        <section className="employee-dashboard-section" aria-labelledby="employee-approvals-title">
          <div className="section-heading">
            <div><p className="eyebrow">Decisions</p><h2 id="employee-approvals-title">Approvals waiting for you</h2></div>
            <ShieldCheck size={18} aria-hidden="true" />
          </div>
          {errors.approvals && <p className="task-manage-feedback error" role="alert">{errors.approvals}</p>}
          {approvalError && <p className="task-manage-feedback error" role="alert">{approvalError}</p>}
          {!loading && approvals.length === 0 ? (
            <p className="employee-dashboard-empty">No approvals are waiting for your decision.</p>
          ) : (
            <ul className="employee-dashboard-list">
              {approvals.map((approval) => (
                <li className="employee-dashboard-item" key={approval.id}>
                  <div>
                    <strong>{approval.summary}</strong>
                    <span>Requested by {approval.requester_name}</span>
                  </div>
                  {approval.can_decide && (
                    <div className="employee-dashboard-approval-actions">
                      <span className="status-pill warning">Waiting</span>
                      <div className="employee-dashboard-actions">
                        <button
                          className="button secondary"
                          type="button"
                          disabled={busyApprovalId === approval.id}
                          onClick={() => void decide(approval, 'reject')}
                        >Reject</button>
                        <button
                          className="button primary"
                          type="button"
                          disabled={busyApprovalId === approval.id}
                          onClick={() => void decide(approval, 'approve')}
                        >Approve</button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}
