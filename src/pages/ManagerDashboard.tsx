import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  CheckSquare,
  Clock3,
  RefreshCw,
  TrendingUp,
  Users,
} from 'lucide-react'
import { getTeamDashboard, type TeamDashboardData, type TeamOverdueTask } from '../api/tasks'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'

const REFRESH_INTERVAL_MS = 3000

function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function formatOverdueDuration(hours: number): string {
  if (hours <= 0) return 'Just overdue'
  const days = Math.floor(hours / 24)
  const remainingHours = hours % 24
  if (days === 0) return `${hours}h overdue`
  if (remainingHours === 0) return `${days}d overdue`
  return `${days}d ${remainingHours}h overdue`
}

function priorityClass(priority: string): string {
  switch (priority.toLowerCase()) {
    case 'urgent':
      return 'priority-pill urgent'
    case 'high':
      return 'priority-pill high'
    case 'medium':
      return 'priority-pill medium'
    case 'low':
      return 'priority-pill low'
    default:
      return 'priority-pill'
  }
}

export function ManagerDashboard() {
  const { user } = useAuth()
  const [data, setData] = useState<TeamDashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const isEmployee = user?.role === 'employee'

  const loadData = useCallback(async (isManualRefresh = false) => {
    if (isEmployee) return
    if (isManualRefresh) setRefreshing(true)

    try {
      const response = await getTeamDashboard()
      setData(response)
      setError(null)
      setLastUpdated(new Date())
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load team dashboard'
      setError(msg)
    } finally {
      setLoading(false)
      if (isManualRefresh) setRefreshing(false)
    }
  }, [isEmployee])

  useEffect(() => {
    if (!isEmployee) {
      void loadData()
    }
  }, [isEmployee, loadData])

  // Automatic live refresh every 3 seconds
  usePolling(loadData, REFRESH_INTERVAL_MS, !isEmployee)

  // Sort overdue tasks oldest overdue first (highest overdue_hours, earliest deadline)
  const sortedOverdueTasks = useMemo(() => {
    if (!data?.overdue_tasks) return []
    return [...data.overdue_tasks].sort((a: TeamOverdueTask, b: TeamOverdueTask) => {
      if (b.overdue_hours !== a.overdue_hours) {
        return b.overdue_hours - a.overdue_hours
      }
      return new Date(a.due_at).getTime() - new Date(b.due_at).getTime()
    })
  }, [data?.overdue_tasks])

  // Prevent employees from accessing manager dashboard
  if (isEmployee) {
    return <AccessDenied />
  }

  // Initial loading state
  if (loading && !data) {
    return (
      <div className="page-stack manager-dashboard">
        <section className="state-card" data-testid="dashboard-loading" aria-live="polite">
          <div className="state-icon" aria-hidden="true">
            <RefreshCw className="spin-animation" size={28} />
          </div>
          <h2>Loading team dashboard…</h2>
          <p>Retrieving team statistics, workload, and deadlines.</p>
        </section>
      </div>
    )
  }

  // Error state when no data could be loaded
  if (error && !data) {
    return (
      <div className="page-stack manager-dashboard">
        <section className="state-card error-card" data-testid="dashboard-error" aria-live="assertive">
          <div className="state-icon danger" aria-hidden="true">
            <AlertCircle size={28} />
          </div>
          <h2>Unable to load team dashboard</h2>
          <p>{error}</p>
          <button
            type="button"
            className="button primary-button"
            onClick={() => void loadData(true)}
          >
            Retry
          </button>
        </section>
      </div>
    )
  }

  const summary = data?.summary
  const members = data?.members ?? []

  return (
    <div className="page-stack manager-dashboard">
      {/* Background refresh error banner */}
      {error && data && (
        <div className="manager-dashboard-alert" role="alert" data-testid="dashboard-error-banner">
          <AlertCircle size={16} aria-hidden="true" />
          <span>Could not refresh data: {error}</span>
          <button
            type="button"
            className="text-button"
            onClick={() => void loadData(true)}
          >
            Retry
          </button>
        </div>
      )}

      {/* A. Dashboard Header */}
      <section className="hero-card manager-hero" aria-labelledby="manager-dashboard-title">
        <div className="manager-hero-info">
          <div className="manager-hero-tags">
            <span className="eyebrow light">Team Lead &amp; Manager Workspace</span>
            {summary && (
              <span className="manager-team-badge" data-testid="team-name-badge">
                {summary.team_name}
              </span>
            )}
          </div>
          <h2 id="manager-dashboard-title">Team Overview</h2>
          <p className="manager-hero-subtitle">
            Monitor team performance, workload and deadlines.
          </p>
          <div className="manager-header-meta">
            {summary && (
              <span className="manager-meta-item" data-testid="team-members-count">
                <Users size={14} aria-hidden="true" />
                {summary.total_members} {summary.total_members === 1 ? 'member' : 'members'}
              </span>
            )}
            {lastUpdated && (
              <span className="manager-meta-item" data-testid="last-updated-time">
                Last updated: {lastUpdated.toLocaleTimeString()}
              </span>
            )}
            <span className="manager-live-indicator" title="Auto-refreshing every 3 seconds">
              <span className="live-dot" aria-hidden="true" />
              Live
            </span>
          </div>
        </div>

        <div className="hero-actions">
          <button
            className="button ghost-button"
            type="button"
            onClick={() => void loadData(true)}
            disabled={refreshing}
            aria-label="Refresh dashboard"
          >
            <RefreshCw
              size={16}
              className={refreshing ? 'spin-animation' : undefined}
              aria-hidden="true"
            />
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </section>

      {/* B. Team Statistics Cards */}
      {summary && (
        <section
          className="manager-stats-grid"
          aria-label="Team Statistics"
          data-testid="team-statistics"
        >
          {/* 1. Total Tasks */}
          <div className="manager-stat-card" data-testid="stat-total-tasks">
            <div className="stat-card-icon stat-icon-blue">
              <CheckSquare size={20} aria-hidden="true" />
            </div>
            <div className="stat-card-content">
              <span className="stat-card-value">{summary.total_tasks}</span>
              <span className="stat-card-label">Total Tasks</span>
            </div>
          </div>

          {/* 2. Open Tasks */}
          <div className="manager-stat-card" data-testid="stat-open-tasks">
            <div className="stat-card-icon stat-icon-navy">
              <Clock3 size={20} aria-hidden="true" />
            </div>
            <div className="stat-card-content">
              <span className="stat-card-value">{summary.open_tasks}</span>
              <span className="stat-card-label">Open Tasks</span>
            </div>
          </div>

          {/* 3. Due Soon */}
          <div className="manager-stat-card" data-testid="stat-due-soon-tasks">
            <div className="stat-card-icon stat-icon-amber">
              <CalendarDays size={20} aria-hidden="true" />
            </div>
            <div className="stat-card-content">
              <span className="stat-card-value">{summary.due_soon_tasks}</span>
              <span className="stat-card-label">Due Soon (&lt;24h)</span>
            </div>
          </div>

          {/* 4. Overdue Tasks */}
          <div
            className={`manager-stat-card ${summary.overdue_tasks > 0 ? 'stat-card-overdue-warning' : ''}`}
            data-testid="stat-overdue-tasks"
          >
            <div className={`stat-card-icon ${summary.overdue_tasks > 0 ? 'stat-icon-danger' : 'stat-icon-muted'}`}>
              <AlertTriangle size={20} aria-hidden="true" />
            </div>
            <div className="stat-card-content">
              <span className="stat-card-value">{summary.overdue_tasks}</span>
              <span className="stat-card-label">Overdue Tasks</span>
            </div>
          </div>

          {/* 5. Completion Rate */}
          <div className="manager-stat-card" data-testid="stat-completion-rate">
            <div className="stat-card-icon stat-icon-green">
              <TrendingUp size={20} aria-hidden="true" />
            </div>
            <div className="stat-card-content">
              <span className="stat-card-value">{Number(summary.completion_rate).toFixed(1)}%</span>
              <span className="stat-card-label">Completion Rate</span>
            </div>
          </div>
        </section>
      )}

      {/* C. Team Workload Table */}
      <section className="manager-card" aria-labelledby="workload-heading" data-testid="workload-section">
        <div className="manager-card-header">
          <div>
            <h3 id="workload-heading">Team Workload</h3>
            <p className="manager-card-sub">Individual breakdown of tasks and completion progress across team members.</p>
          </div>
        </div>

        {members.length === 0 ? (
          <div className="manager-empty-state" data-testid="empty-members-state">
            <Users size={32} aria-hidden="true" />
            <h4>No team members</h4>
            <p>There are no active employees assigned to this team.</p>
          </div>
        ) : (
          <div className="manager-table-wrap">
            <table className="manager-table" aria-label="Team Workload Table">
              <thead>
                <tr>
                  <th scope="col">Employee</th>
                  <th scope="col" className="text-right">Total</th>
                  <th scope="col" className="text-right">Open</th>
                  <th scope="col" className="text-right">Due Soon</th>
                  <th scope="col" className="text-right">Overdue</th>
                  <th scope="col" className="text-right">Completed</th>
                  <th scope="col" className="col-completion">Completion</th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <tr key={member.employee_id} data-testid={`workload-row-${member.employee_id}`}>
                    <td className="cell-employee">
                      <strong className="employee-name">{member.employee_name}</strong>
                      <span className="employee-email">{member.employee_email}</span>
                    </td>
                    <td className="text-right">{member.total_tasks}</td>
                    <td className="text-right">{member.open_tasks}</td>
                    <td className="text-right">
                      {member.due_soon_tasks > 0 ? (
                        <span className="status-pill warning">{member.due_soon_tasks}</span>
                      ) : (
                        <span>0</span>
                      )}
                    </td>
                    <td className="text-right">
                      {member.overdue_tasks > 0 ? (
                        <span className="status-pill danger">{member.overdue_tasks}</span>
                      ) : (
                        <span>0</span>
                      )}
                    </td>
                    <td className="text-right">{member.completed_tasks}</td>
                    <td className="cell-completion">
                      <div className="completion-wrap">
                        <span className="completion-percent">{Number(member.completion_rate).toFixed(1)}%</span>
                        <div
                          className="progress-bar-track"
                          role="progressbar"
                          aria-valuenow={member.completion_rate}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-label={`${member.employee_name} completion rate: ${member.completion_rate}%`}
                        >
                          <div
                            className={`progress-bar-fill ${member.completion_rate === 100 ? 'complete' : ''}`}
                            style={{ width: `${Math.min(100, Math.max(0, member.completion_rate))}%` }}
                          />
                        </div>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* D. Separate Overdue Tasks Section */}
      <section className="manager-card" aria-labelledby="overdue-heading" data-testid="overdue-section">
        <div className="manager-card-header">
          <div>
            <h3 id="overdue-heading">
              Overdue Tasks
              {sortedOverdueTasks.length > 0 && (
                <span className="overdue-badge-count">{sortedOverdueTasks.length}</span>
              )}
            </h3>
            <p className="manager-card-sub">Tasks that have passed their deadline and require follow-up.</p>
          </div>
        </div>

        {sortedOverdueTasks.length === 0 ? (
          <div className="manager-empty-state" data-testid="empty-overdue-state">
            <CheckCircle2 size={32} className="text-success" aria-hidden="true" />
            <h4>No overdue tasks</h4>
            <p>All assigned tasks are on track. Great job keeping the team ahead of schedule!</p>
          </div>
        ) : (
          <div className="manager-table-wrap">
            <table className="manager-table" aria-label="Overdue Tasks Table">
              <thead>
                <tr>
                  <th scope="col">Task</th>
                  <th scope="col">Assigned To</th>
                  <th scope="col">Deadline</th>
                  <th scope="col">Overdue Duration</th>
                  <th scope="col">Priority</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {sortedOverdueTasks.map((task) => (
                  <tr key={task.task_id} data-testid={`overdue-row-${task.task_id}`}>
                    <td className="cell-task-title">
                      <strong>{task.title}</strong>
                    </td>
                    <td className="cell-assignee">
                      <span>{task.assignee_name}</span>
                      <small className="task-cell-note">{task.assignee_email}</small>
                    </td>
                    <td>{formatDateTime(task.due_at)}</td>
                    <td>
                      <span className="overdue-duration-pill">
                        <AlertTriangle size={12} aria-hidden="true" />
                        {formatOverdueDuration(task.overdue_hours)}
                      </span>
                    </td>
                    <td>
                      <span className={priorityClass(task.priority)}>
                        {task.priority}
                      </span>
                    </td>
                    <td>
                      <span className="status-pill warning">
                        {task.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
