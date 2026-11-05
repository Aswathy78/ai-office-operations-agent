import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowDownToLine,
  Calendar,
  CheckCircle2,
  Clock,
  ExternalLink,
  Filter,
  Mail,
  RefreshCw,
  Search,
  ShieldAlert,
  Workflow,
} from 'lucide-react'
import {
  getOperationalMonitoring,
  type ActionItemMonitorRow,
  type FailureReasonRow,
  type OperationalMonitoringData,
  type TaskStatusRow,
  type WorkflowFailureRow,
} from '../api/tasks'
import { API_BASE_URL, apiFetch } from '../api/client'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'

const REFRESH_INTERVAL_MS = 5000

function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
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

function statusBadgeClass(status: string | null): string {
  if (!status) return 'status-badge neutral'
  switch (status.toLowerCase()) {
    case 'completed':
    case 'sent':
      return 'status-badge sent'
    case 'pending':
    case 'pending_review':
      return 'status-badge pending'
    case 'failed':
    case 'rejected':
      return 'status-badge failed'
    case 'retrying':
      return 'status-badge retrying'
    default:
      return 'status-badge'
  }
}

function categoryBadgeClass(category: string): string {
  switch (category.toLowerCase()) {
    case 'email':
      return 'category-pill email'
    case 'calendar':
      return 'category-pill calendar'
    case 'workflow':
      return 'category-pill workflow'
    case 'notification':
      return 'category-pill notification'
    default:
      return 'category-pill'
  }
}

type TabType = 'all' | 'action_items' | 'tasks' | 'workflows' | 'reasons'

export function ManagerMonitoringPage() {
  const { user } = useAuth()
  const [data, setData] = useState<OperationalMonitoringData | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [activeTab, setActiveTab] = useState<TabType>('all')
  const [searchQuery, setSearchQuery] = useState('')
  const [taskFilter, setTaskFilter] = useState<'all' | 'failures' | 'pending'>('all')

  const isEmployee = user?.role === 'employee'

  const loadData = useCallback(
    async (isManualRefresh = false) => {
      if (isEmployee) return
      if (isManualRefresh) setRefreshing(true)

      try {
        const response = await getOperationalMonitoring()
        setData(response)
        setError(null)
        setLastUpdated(new Date())
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to load monitoring data'
        setError(msg)
      } finally {
        setLoading(false)
        if (isManualRefresh) setRefreshing(false)
      }
    },
    [isEmployee],
  )

  useEffect(() => {
    if (!isEmployee) {
      void loadData()
    }
  }, [isEmployee, loadData])

  usePolling(loadData, REFRESH_INTERVAL_MS, !isEmployee)

  const handleExportCsv = async () => {
    try {
      setExporting(true)
      const res = await apiFetch(`${API_BASE_URL}/api/v1/tasks/operational-monitoring/export.csv`)
      if (!res.ok) {
        throw new Error(`Export failed with status ${res.status}`)
      }
      const blob = await res.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `task-operational-monitoring-${new Date().toISOString().slice(0, 10)}.csv`
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to export CSV'
      setError(msg)
    } finally {
      setExporting(false)
    }
  }

  // Filtered action items
  const filteredActionItems = useMemo(() => {
    if (!data?.action_items) return []
    if (!searchQuery.trim()) return data.action_items
    const q = searchQuery.toLowerCase()
    return data.action_items.filter(
      (item) =>
        item.task_description.toLowerCase().includes(q) ||
        (item.owner_name && item.owner_name.toLowerCase().includes(q)) ||
        item.meeting_title.toLowerCase().includes(q),
    )
  }, [data?.action_items, searchQuery])

  // Filtered task status rows
  const filteredTasks = useMemo(() => {
    if (!data?.task_status_rows) return []
    let list = data.task_status_rows

    if (taskFilter === 'failures') {
      list = list.filter(
        (t) =>
          t.email_status === 'failed' ||
          t.email_status === 'retrying' ||
          t.calendar_status === 'failed' ||
          t.calendar_status === 'retrying',
      )
    } else if (taskFilter === 'pending') {
      list = list.filter((t) => t.status === 'pending')
    }

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase()
      list = list.filter(
        (t) =>
          t.title.toLowerCase().includes(q) ||
          t.assignee_name.toLowerCase().includes(q) ||
          t.assignee_email.toLowerCase().includes(q),
      )
    }
    return list
  }, [data?.task_status_rows, taskFilter, searchQuery])

  // Filtered workflow failures
  const filteredWorkflowFailures = useMemo(() => {
    if (!data?.workflow_failures) return []
    if (!searchQuery.trim()) return data.workflow_failures
    const q = searchQuery.toLowerCase()
    return data.workflow_failures.filter(
      (w) =>
        w.workflow_type.toLowerCase().includes(q) ||
        w.run_id.toLowerCase().includes(q) ||
        (w.last_error && w.last_error.toLowerCase().includes(q)),
    )
  }, [data?.workflow_failures, searchQuery])

  // Filtered failure reasons
  const filteredFailureReasons = useMemo(() => {
    if (!data?.failure_reasons) return []
    if (!searchQuery.trim()) return data.failure_reasons
    const q = searchQuery.toLowerCase()
    return data.failure_reasons.filter(
      (r) =>
        r.category.toLowerCase().includes(q) ||
        r.error_message.toLowerCase().includes(q),
    )
  }, [data?.failure_reasons, searchQuery])

  if (isEmployee) {
    return <AccessDenied />
  }

  return (
    <div className="manager-dashboard-container monitoring-page">
      {/* Top Header */}
      <header className="manager-header monitoring-header">
        <div className="manager-header-title-group">
          <div className="manager-header-title-row">
            <h1 className="manager-page-title">Operations Monitoring Portal</h1>
            {data?.summary && (
              <span className="team-badge" data-testid="team-badge">
                {data.summary.team_name}
              </span>
            )}
            {data?.summary && data.summary.total_failures > 0 ? (
              <span className="monitoring-alert-pill" data-testid="total-failures-badge">
                <AlertTriangle className="pill-icon" size={14} />
                {data.summary.total_failures} Active Failure{data.summary.total_failures === 1 ? '' : 's'}
              </span>
            ) : data?.summary ? (
              <span className="monitoring-healthy-pill" data-testid="healthy-badge">
                <CheckCircle2 className="pill-icon" size={14} />
                All Operational Systems Normal
              </span>
            ) : null}
          </div>
          <p className="manager-page-subtitle">
            Live monitoring for action items, email & calendar delivery, and workflow root causes
          </p>
        </div>

        <div className="manager-header-actions">
          <div className="live-indicator-wrapper" title="Live updates active every 5s">
            <span className="live-dot" />
            <span className="live-text">Live</span>
          </div>

          {lastUpdated && (
            <span className="last-updated-text" title="Last data fetch">
              <Clock size={13} style={{ marginRight: 4, display: 'inline' }} />
              {lastUpdated.toLocaleTimeString()}
            </span>
          )}

          <button
            type="button"
            className="manager-btn-refresh"
            onClick={() => void loadData(true)}
            disabled={refreshing}
            aria-label="Refresh monitoring data"
            title="Refresh now"
            data-testid="refresh-monitoring-btn"
          >
            <RefreshCw size={15} className={refreshing ? 'spin' : ''} />
            <span>Refresh</span>
          </button>

          <button
            type="button"
            className="manager-btn-export"
            onClick={() => void handleExportCsv()}
            disabled={exporting}
            aria-label="Export task monitoring table as CSV"
            title="Download CSV export"
            data-testid="export-csv-btn"
          >
            <ArrowDownToLine size={15} />
            <span>{exporting ? 'Exporting...' : 'Export CSV'}</span>
          </button>
        </div>
      </header>

      {/* Error Alert */}
      {error && (
        <div className="manager-error-banner" role="alert">
          <AlertCircle size={18} />
          <span>{error}</span>
          <button
            type="button"
            className="manager-error-retry"
            onClick={() => void loadData(true)}
          >
            Retry
          </button>
        </div>
      )}

      {/* KPI Summary Cards */}
      <section className="manager-kpi-grid monitoring-kpi-grid" aria-label="Monitoring summary KPIs">
        <div className="manager-kpi-card" data-testid="kpi-action-items">
          <div className="manager-kpi-header">
            <span className="manager-kpi-label">Action Items</span>
            <div className="manager-kpi-icon-wrapper info">
              <Activity size={18} />
            </div>
          </div>
          <div className="manager-kpi-value-row">
            <span className="manager-kpi-value">
              {loading ? '—' : (data?.summary.total_action_items ?? 0)}
            </span>
            {data?.summary && data.summary.action_items_pending_review > 0 && (
              <span className="monitoring-kpi-sub-pill pending" title="Pending review">
                {data.summary.action_items_pending_review} to review
              </span>
            )}
          </div>
          <span className="manager-kpi-hint">Meeting-derived deliverables</span>
        </div>

        <div className="manager-kpi-card" data-testid="kpi-tasks">
          <div className="manager-kpi-header">
            <span className="manager-kpi-label">Monitored Tasks</span>
            <div className="manager-kpi-icon-wrapper primary">
              <Calendar size={18} />
            </div>
          </div>
          <div className="manager-kpi-value-row">
            <span className="manager-kpi-value">
              {loading ? '—' : (data?.summary.total_tasks_monitored ?? 0)}
            </span>
          </div>
          <span className="manager-kpi-hint">Assigned across team members</span>
        </div>

        <div className="manager-kpi-card" data-testid="kpi-email-failures">
          <div className="manager-kpi-header">
            <span className="manager-kpi-label">Email Failures</span>
            <div className={`manager-kpi-icon-wrapper ${data?.summary.email_failed_count ? 'danger' : 'success'}`}>
              <Mail size={18} />
            </div>
          </div>
          <div className="manager-kpi-value-row">
            <span className={`manager-kpi-value ${data?.summary.email_failed_count ? 'danger-text' : ''}`}>
              {loading ? '—' : (data?.summary.email_failed_count ?? 0)}
            </span>
          </div>
          <span className="manager-kpi-hint">Assignment notifications</span>
        </div>

        <div className="manager-kpi-card" data-testid="kpi-calendar-failures">
          <div className="manager-kpi-header">
            <span className="manager-kpi-label">Calendar Sync Failures</span>
            <div className={`manager-kpi-icon-wrapper ${data?.summary.calendar_failed_count ? 'danger' : 'success'}`}>
              <Calendar size={18} />
            </div>
          </div>
          <div className="manager-kpi-value-row">
            <span className={`manager-kpi-value ${data?.summary.calendar_failed_count ? 'danger-text' : ''}`}>
              {loading ? '—' : (data?.summary.calendar_failed_count ?? 0)}
            </span>
          </div>
          <span className="manager-kpi-hint">Provider synchronisations</span>
        </div>

        <div className="manager-kpi-card" data-testid="kpi-workflow-failures">
          <div className="manager-kpi-header">
            <span className="manager-kpi-label">Workflow Failures</span>
            <div className={`manager-kpi-icon-wrapper ${data?.summary.workflow_failed_count ? 'danger' : 'success'}`}>
              <Workflow size={18} />
            </div>
          </div>
          <div className="manager-kpi-value-row">
            <span className={`manager-kpi-value ${data?.summary.workflow_failed_count ? 'danger-text' : ''}`}>
              {loading ? '—' : (data?.summary.workflow_failed_count ?? 0)}
            </span>
          </div>
          <span className="manager-kpi-hint">Background run deadlocks</span>
        </div>

        <div className="manager-kpi-card" data-testid="kpi-total-failures">
          <div className="manager-kpi-header">
            <span className="manager-kpi-label">Total Failures</span>
            <div className={`manager-kpi-icon-wrapper ${data?.summary.total_failures ? 'danger' : 'success'}`}>
              <ShieldAlert size={18} />
            </div>
          </div>
          <div className="manager-kpi-value-row">
            <span className={`manager-kpi-value ${data?.summary.total_failures ? 'danger-text' : 'success-text'}`}>
              {loading ? '—' : (data?.summary.total_failures ?? 0)}
            </span>
          </div>
          <span className="manager-kpi-hint">Root causes needing attention</span>
        </div>
      </section>

      {/* Navigation Tabs & Search Toolbar */}
      <div className="monitoring-toolbar">
        <div className="monitoring-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'all'}
            className={`monitoring-tab ${activeTab === 'all' ? 'active' : ''}`}
            onClick={() => setActiveTab('all')}
            data-testid="tab-all"
          >
            All Views
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'action_items'}
            className={`monitoring-tab ${activeTab === 'action_items' ? 'active' : ''}`}
            onClick={() => setActiveTab('action_items')}
            data-testid="tab-action-items"
          >
            Action Items ({data?.action_items.length ?? 0})
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'tasks'}
            className={`monitoring-tab ${activeTab === 'tasks' ? 'active' : ''}`}
            onClick={() => setActiveTab('tasks')}
            data-testid="tab-task-statuses"
          >
            Task Operational Status ({data?.task_status_rows.length ?? 0})
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'workflows'}
            className={`monitoring-tab ${activeTab === 'workflows' ? 'active' : ''}`}
            onClick={() => setActiveTab('workflows')}
            data-testid="tab-workflow-failures"
          >
            Workflow Failures ({data?.workflow_failures.length ?? 0})
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'reasons'}
            className={`monitoring-tab ${activeTab === 'reasons' ? 'active' : ''}`}
            onClick={() => setActiveTab('reasons')}
            data-testid="tab-failure-reasons"
          >
            Failure Reasons ({data?.failure_reasons.length ?? 0})
          </button>
        </div>

        <div className="monitoring-search-box">
          <Search size={15} className="search-icon" />
          <input
            type="text"
            className="monitoring-search-input"
            placeholder="Search tasks, owners, workflows or error messages..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            aria-label="Search monitoring records"
            data-testid="monitoring-search-input"
          />
        </div>
      </div>

      {/* SECTION 1: Action Items Table */}
      {(activeTab === 'all' || activeTab === 'action_items') && (
        <section className="manager-section monitoring-section" aria-labelledby="action-items-heading">
          <div className="manager-section-header">
            <div>
              <h2 id="action-items-heading" className="manager-section-title">
                Meeting Action Items
              </h2>
              <p className="manager-section-subtitle">
                Owner assignments, due deadlines, and review states extracted from team meetings
              </p>
            </div>
            <span className="count-pill">{filteredActionItems.length} items</span>
          </div>

          <div className="manager-table-wrapper monitoring-table-wrap">
            <table className="manager-table" aria-label="Meeting Action Items table">
              <thead>
                <tr>
                  <th scope="col">Action Item</th>
                  <th scope="col">Owner</th>
                  <th scope="col">Deadline</th>
                  <th scope="col">Meeting</th>
                  <th scope="col">Ownership State</th>
                  <th scope="col">Review State</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={6} className="manager-empty-state">
                      Loading action items...
                    </td>
                  </tr>
                ) : filteredActionItems.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="manager-empty-state" data-testid="empty-action-items">
                      No meeting action items found for this team.
                    </td>
                  </tr>
                ) : (
                  filteredActionItems.map((item) => (
                    <tr key={item.action_item_id} data-testid={`action-item-row-${item.action_item_id}`}>
                      <td className="task-title-cell font-medium">{item.task_description}</td>
                      <td>
                        <div className="employee-info-cell">
                          <span className="employee-name">{item.owner_name || 'Unassigned'}</span>
                          {item.owner_email && (
                            <span className="employee-email">{item.owner_email}</span>
                          )}
                        </div>
                      </td>
                      <td className="due-date-cell">{formatDateTime(item.deadline_at)}</td>
                      <td>
                        <span className="meeting-title-badge" title={`Meeting ID: ${item.meeting_id}`}>
                          {item.meeting_title}
                        </span>
                      </td>
                      <td>
                        <span className={`status-badge ${item.ownership_status}`}>
                          {item.ownership_status}
                        </span>
                      </td>
                      <td>
                        <span
                          className={statusBadgeClass(item.review_status)}
                          data-testid={`review-status-${item.action_item_id}`}
                        >
                          {item.review_status}
                        </span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* SECTION 2: Task Operational Status Table (Email + Calendar) */}
      {(activeTab === 'all' || activeTab === 'tasks') && (
        <section className="manager-section monitoring-section" aria-labelledby="task-status-heading">
          <div className="manager-section-header">
            <div>
              <h2 id="task-status-heading" className="manager-section-title">
                Task Status & Delivery Channels
              </h2>
              <p className="manager-section-subtitle">
                Comprehensive tracking of task status, email dispatch, and calendar synchronization
              </p>
            </div>
            <div className="task-filter-group">
              <Filter size={14} style={{ marginRight: 4 }} />
              <button
                type="button"
                className={`filter-btn ${taskFilter === 'all' ? 'active' : ''}`}
                onClick={() => setTaskFilter('all')}
              >
                All ({data?.task_status_rows.length ?? 0})
              </button>
              <button
                type="button"
                className={`filter-btn ${taskFilter === 'failures' ? 'active' : ''}`}
                onClick={() => setTaskFilter('failures')}
                data-testid="filter-failures-btn"
              >
                Failures Only
              </button>
              <button
                type="button"
                className={`filter-btn ${taskFilter === 'pending' ? 'active' : ''}`}
                onClick={() => setTaskFilter('pending')}
              >
                Open Only
              </button>
            </div>
          </div>

          <div className="manager-table-wrapper monitoring-table-wrap">
            <table className="manager-table" aria-label="Task Operational Status table">
              <thead>
                <tr>
                  <th scope="col">Task</th>
                  <th scope="col">Assignee</th>
                  <th scope="col">Priority</th>
                  <th scope="col">Due Date</th>
                  <th scope="col">Status</th>
                  <th scope="col">Email Dispatch</th>
                  <th scope="col">Calendar Sync</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={7} className="manager-empty-state">
                      Loading task statuses...
                    </td>
                  </tr>
                ) : filteredTasks.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="manager-empty-state" data-testid="empty-tasks">
                      No matching tasks found.
                    </td>
                  </tr>
                ) : (
                  filteredTasks.map((t) => (
                    <tr key={t.task_id} data-testid={`task-status-row-${t.task_id}`}>
                      <td className="task-title-cell font-medium">{t.title}</td>
                      <td>
                        <div className="employee-info-cell">
                          <span className="employee-name">{t.assignee_name}</span>
                          <span className="employee-email">{t.assignee_email}</span>
                        </div>
                      </td>
                      <td>
                        <span className={priorityClass(t.priority)}>{t.priority}</span>
                      </td>
                      <td className="due-date-cell">{formatDateTime(t.due_at)}</td>
                      <td>
                        <span className={statusBadgeClass(t.status)}>{t.status}</span>
                      </td>
                      <td>
                        <div className="channel-status-cell">
                          <span
                            className={statusBadgeClass(t.email_status)}
                            data-testid={`email-status-${t.task_id}`}
                          >
                            {t.email_status || 'not queued'}
                          </span>
                          {t.email_error && (
                            <span className="channel-error-msg" title={t.email_error}>
                              {t.email_error}
                            </span>
                          )}
                        </div>
                      </td>
                      <td>
                        <div className="channel-status-cell">
                          <span
                            className={statusBadgeClass(t.calendar_status)}
                            data-testid={`calendar-status-${t.task_id}`}
                          >
                            {t.calendar_status || 'not synced'}
                          </span>
                          {t.calendar_provider && (
                            <span className="provider-pill">
                              {t.calendar_provider}
                              {t.calendar_attempt_count ? ` (${t.calendar_attempt_count}x)` : ''}
                            </span>
                          )}
                          {t.calendar_error && (
                            <span className="channel-error-msg" title={t.calendar_error}>
                              {t.calendar_error}
                            </span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* SECTION 3: Workflow Failures Table */}
      {(activeTab === 'all' || activeTab === 'workflows') && (
        <section className="manager-section monitoring-section" aria-labelledby="workflow-failures-heading">
          <div className="manager-section-header">
            <div>
              <h2 id="workflow-failures-heading" className="manager-section-title">
                Workflow Failures
              </h2>
              <p className="manager-section-subtitle">
                Background jobs and pipeline executions requiring intervention
              </p>
            </div>
            <span className="count-pill danger">{filteredWorkflowFailures.length} failed</span>
          </div>

          <div className="manager-table-wrapper monitoring-table-wrap">
            <table className="manager-table" aria-label="Workflow Failures table">
              <thead>
                <tr>
                  <th scope="col">Workflow Type</th>
                  <th scope="col">Run ID</th>
                  <th scope="col">Status</th>
                  <th scope="col">Failure Reason</th>
                  <th scope="col">Attempts</th>
                  <th scope="col">Last Attempt</th>
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={7} className="manager-empty-state">
                      Loading workflow failures...
                    </td>
                  </tr>
                ) : filteredWorkflowFailures.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="manager-empty-state" data-testid="empty-workflows">
                      <CheckCircle2 size={18} style={{ marginRight: 6, display: 'inline', color: 'var(--color-success, #16a34a)' }} />
                      No workflow failures detected. All background processes running normally.
                    </td>
                  </tr>
                ) : (
                  filteredWorkflowFailures.map((wf) => (
                    <tr key={wf.run_id} data-testid={`workflow-failure-row-${wf.run_id}`}>
                      <td className="font-medium">{wf.workflow_type}</td>
                      <td>
                        <code className="run-id-code">{wf.run_id}</code>
                      </td>
                      <td>
                        <span className={`status-badge ${wf.status}`}>{wf.status}</span>
                      </td>
                      <td className="error-detail-cell">
                        <span className="error-text" title={wf.last_error || 'No detail'}>
                          {wf.last_error || 'Unknown failure'}
                        </span>
                      </td>
                      <td>
                        <span className="attempt-badge">
                          {wf.attempt_count} / {wf.max_attempts}
                        </span>
                      </td>
                      <td className="due-date-cell">{formatDateTime(wf.updated_at)}</td>
                      <td>
                        <Link
                          to={wf.run_link}
                          className="workflow-run-link"
                          title="Inspect run in workflow queue"
                          data-testid={`workflow-link-${wf.run_id}`}
                        >
                          <span>Inspect Run</span>
                          <ExternalLink size={13} />
                        </Link>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* SECTION 4: Failure Root Cause & Reason Table (Why failures occur) */}
      {(activeTab === 'all' || activeTab === 'reasons') && (
        <section className="manager-section monitoring-section" aria-labelledby="failure-reasons-heading">
          <div className="manager-section-header">
            <div>
              <h2 id="failure-reasons-heading" className="manager-section-title">
                Failure Root Cause & Diagnostic Summary
              </h2>
              <p className="manager-section-subtitle">
                Aggregated breakdown explaining why failures occurred across email, calendar, notifications, and workflows
              </p>
            </div>
            <span className="count-pill">{filteredFailureReasons.length} distinct errors</span>
          </div>

          <div className="manager-table-wrapper monitoring-table-wrap">
            <table className="manager-table" aria-label="Failure Reasons diagnostic table">
              <thead>
                <tr>
                  <th scope="col">Category</th>
                  <th scope="col">Root Cause / Error Message</th>
                  <th scope="col">Occurrences</th>
                  <th scope="col">Last Detected</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={4} className="manager-empty-state">
                      Loading diagnostic failure summary...
                    </td>
                  </tr>
                ) : filteredFailureReasons.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="manager-empty-state" data-testid="empty-failure-reasons">
                      <CheckCircle2 size={18} style={{ marginRight: 6, display: 'inline', color: 'var(--color-success, #16a34a)' }} />
                      Zero operational failures recorded. All services healthy.
                    </td>
                  </tr>
                ) : (
                  filteredFailureReasons.map((reason, idx) => (
                    <tr key={`${reason.category}-${idx}`} data-testid={`failure-reason-row-${idx}`}>
                      <td>
                        <span className={categoryBadgeClass(reason.category)}>
                          {reason.category}
                        </span>
                      </td>
                      <td className="font-mono error-reason-text">
                        {reason.error_message}
                      </td>
                      <td>
                        <span className="failure-count-pill" data-testid={`failure-count-${idx}`}>
                          {reason.count} {reason.count === 1 ? 'incident' : 'incidents'}
                        </span>
                      </td>
                      <td className="due-date-cell">{formatDateTime(reason.last_occurred_at)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}

