// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AdminDashboard } from './api/adminDashboard'
import App from './App'

const { authState, api } = vi.hoisted(() => ({
  authState: { user: { id: 'a', name: 'Ada', email: 'a@x.test', role: 'admin', department: 'IT' }, isAuthenticated: true },
  api: { getAdminDashboard: vi.fn() },
}))

vi.mock('./contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('./api/adminDashboard', () => api)
vi.mock('./api/adminAudit', () => ({ getAuditLog: vi.fn().mockResolvedValue({ entries: [{ id: 'task:1', at: '2026-11-07T10:00:00Z', source: 'task', actor_id: 'e', actor_name: 'Ada Admin', actor_role: null, action: 'create', action_label: 'Task created', target: null, outcome: 'applied', detail: 'Made a task' }], counts: { task: 1, meeting: 0, assistant: 0, email: 0, workflow: 0 }, source_labels: { task: 'Tasks', meeting: 'Meetings', assistant: 'AI assistant', email: 'E-mail', workflow: 'Workflows' }, limit: 200 }) }))
vi.mock('./contexts/AssistantContext', () => ({ AssistantProvider: ({ children }: { children: unknown }) => children }))
vi.mock('./components/layout/AppShell', () => ({ AppShell: ({ children }: { children: unknown }) => children }))
vi.mock('./pages/TaskWorkspacePage', () => ({ TaskWorkspacePage: () => <div>Task dashboard</div> }))

const data = {
  generated_at: '2026-11-07T10:00:00Z',
  refresh_interval_seconds: 30,
  overall_status: 'operational',
  alerts: [],
  users: {
    available: true, error: null, total: 1, active: 1, disabled: 0, by_role: { admin: 1 }, truncated: false,
    items: [{ id: 'u1', name: 'Ada Admin', email: 'ada@example.test', role: 'admin', department: 'IT', is_active: true, last_sign_in_at: null }],
  },
  departments: { available: true, error: null, total: 1, items: [{ name: 'IT', employees: 1, active_employees: 1, with_login: 1 }] },
  integrations: {
    available: true, error: null, window_hours: 24,
    items: [{ key: 'calendar', name: 'Calendar', status: 'operational', detail: 'Calendar entries are in sync.', metrics: {} }],
  },
  system: {
    available: true, error: null,
    database: { latency_ms: 1, schema_current: true },
    api: { window_seconds: 900, observed_seconds: 10, request_count: 5, server_error_count: 0, client_error_count: 0, error_rate_percent: 0, average_latency_ms: 20, p95_latency_ms: 30, max_latency_ms: 40 },
    queue: { depth: 0, due_now: 0, processing: 0, oldest_waiting_seconds: null, failed_24h: 0, dead_letter_open: 0 },
    workers: { configured: 2, alive: 2, started: true },
    background_services: [{ name: 'E-mail dispatcher', running: true }],
  },
} as AdminDashboard

function at(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>)
}

beforeEach(() => {
  authState.user.role = 'admin'
  api.getAdminDashboard.mockReset()
  api.getAdminDashboard.mockResolvedValue(data)
})
afterEach(cleanup)

describe('administrator menus show real content, not a placeholder', () => {
  it('Dashboard: system dashboard with the task dashboard under it', async () => {
    at('/dashboard')
    expect(await screen.findByRole('heading', { name: 'System dashboard' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Users' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Departments' })).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'Calendar' })).toBeInTheDocument()
    expect(screen.getByText('API response time (p95)')).toBeInTheDocument()
    expect(screen.getByText('Job queue depth')).toBeInTheDocument()
    expect(screen.getByText('Active workers')).toBeInTheDocument()
    expect(screen.getByText('Error rate')).toBeInTheDocument()
    expect(screen.getByText('Task dashboard')).toBeInTheDocument()
  })

  it('System dashboard route shows everything', async () => {
    at('/admin/dashboard')
    expect(await screen.findByRole('table', { name: 'Users' })).toBeInTheDocument()
    expect(screen.getByText('Job queue depth')).toBeInTheDocument()
  })

  it('Users menu: users and departments, no placeholder', async () => {
    at('/admin/users')
    expect(await screen.findByRole('heading', { name: 'Users and departments' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Users' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Departments' })).toBeInTheDocument()
    expect(screen.queryByText(/intentionally a placeholder/)).not.toBeInTheDocument()
    expect(screen.queryByText('Job queue depth')).not.toBeInTheDocument()
  })

  it('Agent Monitor menu: health metrics and integrations, no placeholder', async () => {
    at('/admin/agent-monitor')
    expect(await screen.findByRole('heading', { name: 'Agent monitor' })).toBeInTheDocument()
    expect(screen.getByText('API response time (p95)')).toBeInTheDocument()
    expect(screen.getByText('Active workers')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'Calendar' })).toBeInTheDocument()
    expect(screen.queryByText(/intentionally a placeholder/)).not.toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Users' })).not.toBeInTheDocument()
  })

  it('Audit Logs menu: the audit trail, no placeholder', async () => {
    at('/admin/audit-logs')
    expect(await screen.findByRole('table', { name: 'Audit entries' })).toBeInTheDocument()
    expect(screen.getByText('Made a task')).toBeInTheDocument()
    expect(screen.queryByText(/intentionally a placeholder/)).not.toBeInTheDocument()
  })

  it('a manager still sees only the task dashboard and never calls the admin API', async () => {
    authState.user.role = 'manager'
    at('/dashboard')
    expect(await screen.findByText('Task dashboard')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'System dashboard' })).not.toBeInTheDocument()
    expect(api.getAdminDashboard).not.toHaveBeenCalled()
  })

  it('a non-admin is denied the admin menus', async () => {
    authState.user.role = 'employee'
    at('/admin/users')
    expect(await screen.findByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(api.getAdminDashboard).not.toHaveBeenCalled()
  })
})
