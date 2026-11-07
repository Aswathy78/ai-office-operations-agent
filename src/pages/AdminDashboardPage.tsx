import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, RefreshCw } from 'lucide-react'
import {
  getAdminDashboard,
  type AdminDashboard,
  type ApiMetrics,
  type HealthStatus,
  type Section,
} from '../api/adminDashboard'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'

/** The requirement is a refresh at least every 30 seconds; poll well inside that. */
export const REFRESH_INTERVAL_MS = 15_000

const STATUS_LABEL: Record<HealthStatus, string> = {
  operational: 'Operational',
  degraded: 'Degraded',
  down: 'Down',
  not_configured: 'Not configured',
  unknown: 'Unknown',
}

const STATUS_TONE: Record<HealthStatus, string> = {
  operational: '',
  degraded: 'warning',
  down: 'danger',
  not_configured: 'muted',
  unknown: 'muted',
}

const ROLE_LABEL: Record<string, string> = {
  admin: 'Admin',
  manager: 'Manager',
  hr_operations: 'HR / Operations',
  employee: 'Employee',
}

function parseUtc(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
}

function formatTime(iso: string | null): string {
  if (!iso) return 'Never'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parseUtc(iso))
}

function formatClock(date: Date): string {
  return new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' }).format(date)
}

function formatMs(value: number | null): string {
  if (value === null) return 'No data yet'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return 'Nothing waiting'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  return `${(seconds / 3600).toFixed(1)} h`
}

function errorRateText(api: ApiMetrics): string {
  return api.error_rate_percent === null ? 'No data yet' : `${api.error_rate_percent}%`
}

function StatusPill({ status }: { status: HealthStatus }) {
  return <span className={`status-pill ${STATUS_TONE[status]}`}>{STATUS_LABEL[status]}</span>
}

/** Shown in place of any section the API could not read; the rest of the page still works. */
function Unavailable({ title, reason }: { title: string; reason: string }) {
  return (
    <div className="admin-unavailable" role="status">
      <strong>{title} unavailable</strong>
      <span>{reason}</span>
    </div>
  )
}

function Tile({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className={`admin-tile${tone ? ` ${tone}` : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  )
}

function onlyAvailable<T>(section: Section<T>): (T & { available: true }) | null {
  return section.available ? (section as T & { available: true }) : null
}

/** Which part of the dashboard a menu entry shows. The data and refresh are the same. */
export type AdminDashboardView = 'overview' | 'users' | 'health'

const VIEW_TEXT: Record<AdminDashboardView, { title: string; blurb: string }> = {
  overview: { title: 'System dashboard', blurb: 'Users, departments, integrations and system health, measured live.' },
  users: { title: 'Users and departments', blurb: 'Everyone with an account, and how each department is made up.' },
  health: { title: 'Agent monitor', blurb: 'System health and integration status, measured live.' },
}

export function AdminDashboardPage({ view = 'overview' }: { view?: AdminDashboardView }) {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [data, setData] = useState<AdminDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const requestId = useRef(0)

  const load = useCallback(async () => {
    const mine = ++requestId.current
    try {
      const next = await getAdminDashboard()
      if (mine !== requestId.current) return // a newer request already answered
      setData(next)
      setUpdatedAt(new Date())
      setError('')
    } catch (reason) {
      if (mine !== requestId.current) return
      setError(reason instanceof Error ? reason.message : 'The dashboard could not be loaded.')
    } finally {
      if (mine === requestId.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (isAdmin) void load()
  }, [isAdmin, load])

  usePolling(load, REFRESH_INTERVAL_MS, isAdmin)

  if (!isAdmin) return <AccessDenied />

  const users = data ? onlyAvailable(data.users) : null
  const departments = data ? onlyAvailable(data.departments) : null
  const integrations = data ? onlyAvailable(data.integrations) : null
  const system = data ? onlyAvailable(data.system) : null
  const stale = Boolean(data && error)
  const showPeople = view !== 'health'
  const showHealth = view !== 'users'
  const text = VIEW_TEXT[view]

  return (
    <div className="task-workspace admin-dashboard">
      <section className="task-workspace-card" aria-label="System overview">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><Activity size={21} /></div>
          <div>
            <p className="eyebrow">Administration</p>
            <h2>{text.title}</h2>
            <p>
              {text.blurb}
              {updatedAt ? ` Updated ${formatClock(updatedAt)}.` : ''} Refreshes automatically.
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          {data && (
            <span aria-label="Overall system status">
              <StatusPill status={data.overall_status} />
            </span>
          )}
          <button className="button secondary" type="button" onClick={() => void load()}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error && (
        <div className="meetings-error" role="alert">
          <p>
            {stale
              ? `The dashboard could not be refreshed: ${error} Showing the last data from ${
                  updatedAt ? formatClock(updatedAt) : 'earlier'
                }; it may be out of date.`
              : error}
          </p>
          {!data && (
            <button className="button secondary" type="button" onClick={() => void load()}>
              Try again
            </button>
          )}
        </div>
      )}

      {loading && !data && !error && (
        <div className="meetings-loading" role="status">Loading the system dashboard…</div>
      )}

      {data && (
        <>
          {showHealth && data.alerts.length > 0 && (
            <section className="admin-alerts" aria-label="Attention needed">
              <h3>Needs attention</h3>
              <ul>
                {data.alerts.map((alert) => <li key={alert}>{alert}</li>)}
              </ul>
            </section>
          )}

          {showHealth && (<>
          <section className="admin-section" aria-label="System health">
            <h3>System health</h3>
            {!system ? (
              <Unavailable title="System metrics" reason={data.system.error ?? 'They could not be read.'} />
            ) : (
              <>
                <div className="admin-tiles">
                  <Tile
                    label="API response time (p95)"
                    value={formatMs(system.api.p95_latency_ms)}
                    hint={
                      system.api.request_count
                        ? `Average ${formatMs(system.api.average_latency_ms)} over ${system.api.request_count} requests`
                        : 'Starts counting from the first request after the API starts'
                    }
                  />
                  <Tile
                    label="Error rate"
                    value={errorRateText(system.api)}
                    hint={`${system.api.server_error_count} server errors, last ${Math.round(system.api.window_seconds / 60)} min`}
                    tone={(system.api.error_rate_percent ?? 0) >= 5 ? 'danger' : undefined}
                  />
                  <Tile
                    label="Job queue depth"
                    value={String(system.queue.depth)}
                    hint={`${system.queue.processing} running · oldest waiting ${formatDuration(system.queue.oldest_waiting_seconds)}`}
                  />
                  <Tile
                    label="Active workers"
                    value={`${system.workers.alive} of ${system.workers.configured}`}
                    tone={system.workers.alive < system.workers.configured ? 'warning' : undefined}
                    hint={system.workers.started ? 'Workflow workers' : 'Workers have not started'}
                  />
                  <Tile
                    label="Database response"
                    value={formatMs(system.database.latency_ms)}
                    hint={
                      system.database.schema_current === false
                        ? 'Database is behind the code'
                        : system.database.schema_current
                          ? 'Schema up to date'
                          : undefined
                    }
                  />
                  <Tile
                    label="Failed workflows"
                    value={String(system.queue.dead_letter_open)}
                    hint={`${system.queue.failed_24h} failed in the last 24 h`}
                    tone={system.queue.dead_letter_open > 0 ? 'danger' : undefined}
                  />
                </div>
                <ul className="admin-services" aria-label="Background services">
                  {system.background_services.map((service) => (
                    <li key={service.name}>
                      <span>{service.name}</span>
                      <span className={`status-pill ${service.running ? '' : 'danger'}`}>
                        {service.running ? 'Running' : 'Stopped'}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>

          <section className="admin-section" aria-label="Integration health">
            <h3>Integrations</h3>
            {!integrations ? (
              <Unavailable title="Integrations" reason={data.integrations.error ?? 'They could not be read.'} />
            ) : (
              <div className="admin-integrations">
                {integrations.items.map((item) => (
                  <article key={item.key} className="admin-integration" aria-label={item.name}>
                    <header>
                      <strong>{item.name}</strong>
                      <StatusPill status={item.status} />
                    </header>
                    <p>{item.detail}</p>
                  </article>
                ))}
              </div>
            )}
          </section>

          </>)}

          {showPeople && (<>
          <section className="admin-section" aria-label="Users">
            <h3>Users</h3>
            {!users ? (
              <Unavailable title="Users" reason={data.users.error ?? 'They could not be read.'} />
            ) : (
              <>
                <div className="admin-tiles">
                  <Tile label="Total users" value={String(users.total)} />
                  <Tile label="Active" value={String(users.active)} />
                  <Tile label="Disabled" value={String(users.disabled)} />
                  {Object.entries(users.by_role).map(([role, count]) => (
                    <Tile key={role} label={ROLE_LABEL[role] ?? role} value={String(count)} />
                  ))}
                </div>
                {users.items.length === 0 ? (
                  <div className="meetings-empty" role="status"><h3>No users</h3></div>
                ) : (
                  <div className="task-table-wrap">
                    <table className="task-table" aria-label="Users">
                      <thead>
                        <tr>
                          <th>Name</th><th>Role</th><th>Department</th><th>Status</th><th>Last sign-in</th>
                        </tr>
                      </thead>
                      <tbody>
                        {users.items.map((person) => (
                          <tr key={person.id}>
                            <td><strong>{person.name}</strong><small>{person.email}</small></td>
                            <td>{ROLE_LABEL[person.role] ?? person.role}</td>
                            <td>{person.department}</td>
                            <td>
                              <span className={`status-pill ${person.is_active ? '' : 'muted'}`}>
                                {person.is_active ? 'Active' : 'Disabled'}
                              </span>
                            </td>
                            <td>{formatTime(person.last_sign_in_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {users.truncated && (
                  <p className="admin-note">Showing the first {users.items.length} of {users.total} users.</p>
                )}
              </>
            )}
          </section>

          <section className="admin-section" aria-label="Departments">
            <h3>Departments</h3>
            {!departments ? (
              <Unavailable title="Departments" reason={data.departments.error ?? 'They could not be read.'} />
            ) : departments.items.length === 0 ? (
              <div className="meetings-empty" role="status"><h3>No departments</h3></div>
            ) : (
              <div className="task-table-wrap">
                <table className="task-table" aria-label="Departments">
                  <thead>
                    <tr><th>Department</th><th>Employees</th><th>Active</th><th>With a login</th></tr>
                  </thead>
                  <tbody>
                    {departments.items.map((row) => (
                      <tr key={row.name}>
                        <td><strong>{row.name}</strong></td>
                        <td>{row.employees}</td>
                        <td>{row.active_employees}</td>
                        <td>{row.with_login}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          </>)}
        </>
      )}
    </div>
  )
}
