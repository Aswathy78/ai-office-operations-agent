// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AdminDashboard, SystemSection } from '../api/adminDashboard'
import { AdminDashboardPage, REFRESH_INTERVAL_MS } from './AdminDashboardPage'

const { authState, api } = vi.hoisted(() => ({
  authState: { user: { id: 'admin-1', name: 'Ada Admin', role: 'admin' } as { id: string; name: string; role: string } },
  api: { getAdminDashboard: vi.fn() },
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('../api/adminDashboard', () => api)
vi.mock('react-router-dom', () => ({ Link: ({ children }: { children: unknown }) => children }))

const system = (overrides: Partial<SystemSection> = {}): SystemSection => ({
  database: { latency_ms: 1.4, schema_current: true },
  api: {
    window_seconds: 900,
    observed_seconds: 900,
    request_count: 120,
    server_error_count: 3,
    client_error_count: 4,
    error_rate_percent: 2.5,
    average_latency_ms: 42,
    p95_latency_ms: 180,
    max_latency_ms: 900,
  },
  queue: { depth: 7, due_now: 5, processing: 2, oldest_waiting_seconds: 95, failed_24h: 1, dead_letter_open: 1 },
  workers: { configured: 4, alive: 3, started: true },
  background_services: [
    { name: 'E-mail dispatcher', running: true },
    { name: 'Calendar sync', running: false },
  ],
  ...overrides,
})

function dashboard(overrides: Partial<AdminDashboard> = {}): AdminDashboard {
  return {
    generated_at: '2026-11-07T10:00:00Z',
    refresh_interval_seconds: 30,
    overall_status: 'degraded',
    alerts: ['Calendar sync is not running.'],
    users: {
      available: true,
      error: null,
      total: 2,
      active: 1,
      disabled: 1,
      by_role: { admin: 1, employee: 1 },
      truncated: false,
      items: [
        { id: 'u1', name: 'Ada Admin', email: 'ada@example.test', role: 'admin', department: 'IT', is_active: true, last_sign_in_at: '2026-11-07T09:00:00Z' },
        { id: 'u2', name: 'Eli Employee', email: 'eli@example.test', role: 'employee', department: 'Unassigned', is_active: false, last_sign_in_at: null },
      ],
    },
    departments: {
      available: true,
      error: null,
      total: 2,
      items: [
        { name: 'IT', employees: 1, active_employees: 1, with_login: 1 },
        { name: 'Unassigned', employees: 1, active_employees: 0, with_login: 0 },
      ],
    },
    integrations: {
      available: true,
      error: null,
      window_hours: 24,
      items: [
        { key: 'email', name: 'E-mail (SMTP)', status: 'degraded', detail: 'Some e-mail failed in the last 24 hours.', metrics: {} },
        { key: 'ai_model', name: 'AI model (Groq)', status: 'operational', detail: 'Meeting summaries completed in the last 24 hours.', metrics: {} },
        { key: 'calendar', name: 'Calendar', status: 'not_configured', detail: 'No calendar.', metrics: {} },
      ],
    },
    system: { available: true, error: null, ...system() },
    ...overrides,
  }
}

function tile(label: string): HTMLElement {
  return screen.getByText(label).closest('.admin-tile') as HTMLElement
}

beforeEach(() => {
  authState.user = { id: 'admin-1', name: 'Ada Admin', role: 'admin' }
  api.getAdminDashboard.mockReset()
  api.getAdminDashboard.mockResolvedValue(dashboard())
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('AdminDashboardPage access', () => {
  it.each(['employee', 'manager', 'hr_operations'])('refuses a %s without calling the API', (role) => {
    authState.user = { id: 'x', name: 'X', role }
    render(<AdminDashboardPage />)
    expect(screen.getByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(api.getAdminDashboard).not.toHaveBeenCalled()
  })
})

describe('AdminDashboardPage data', () => {
  it('shows a loading state, then the metrics the API measured', async () => {
    let finish: (value: AdminDashboard) => void = () => undefined
    api.getAdminDashboard.mockReturnValueOnce(new Promise<AdminDashboard>((resolve) => { finish = resolve }))
    render(<AdminDashboardPage />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading the system dashboard')

    await act(async () => { finish(dashboard()) })

    expect(await screen.findByText('180 ms')).toBeInTheDocument()
    expect(within(tile('Error rate')).getByText('2.5%')).toBeInTheDocument()
    expect(within(tile('Job queue depth')).getByText('7')).toBeInTheDocument()
    expect(within(tile('Job queue depth')).getByText(/2 running · oldest waiting 2 min/)).toBeInTheDocument()
    expect(within(tile('Active workers')).getByText('3 of 4')).toBeInTheDocument()
    expect(within(tile('Database response')).getByText('1 ms')).toBeInTheDocument()
    expect(screen.queryByText('Loading the system dashboard…')).not.toBeInTheDocument()
  })

  it('lists users, departments and integration health', async () => {
    render(<AdminDashboardPage />)
    const users = await screen.findByRole('table', { name: 'Users' })
    expect(within(users).getByText('ada@example.test')).toBeInTheDocument()
    expect(within(users).getByText('Disabled')).toBeInTheDocument()
    expect(within(users).getByText('Never')).toBeInTheDocument()

    const departments = screen.getByRole('table', { name: 'Departments' })
    expect(within(departments).getByText('Unassigned')).toBeInTheDocument()

    const email = screen.getByRole('article', { name: 'E-mail (SMTP)' })
    expect(within(email).getByText('Degraded')).toBeInTheDocument()
    expect(within(screen.getByRole('article', { name: 'AI model (Groq)' })).getByText('Operational')).toBeInTheDocument()
    expect(within(screen.getByRole('article', { name: 'Calendar' })).getByText('Not configured')).toBeInTheDocument()
  })

  it('calls out what needs attention and which services stopped', async () => {
    render(<AdminDashboardPage />)
    const alerts = await screen.findByRole('region', { name: 'Attention needed' })
    expect(within(alerts).getByText('Calendar sync is not running.')).toBeInTheDocument()
    const services = screen.getByRole('list', { name: 'Background services' })
    expect(within(services).getByText('Stopped')).toBeInTheDocument()
    expect(within(services).getByText('Running')).toBeInTheDocument()
    expect(screen.getByLabelText('Overall system status')).toHaveTextContent('Degraded')
  })

  it('says "No data yet" rather than showing zero when nothing was measured', async () => {
    api.getAdminDashboard.mockResolvedValue(dashboard({
      system: {
        available: true, error: null,
        ...system({
          api: {
            window_seconds: 900, observed_seconds: 5, request_count: 0, server_error_count: 0, client_error_count: 0,
            error_rate_percent: null, average_latency_ms: null, p95_latency_ms: null, max_latency_ms: null,
          },
          queue: { depth: 0, due_now: 0, processing: 0, oldest_waiting_seconds: null, failed_24h: 0, dead_letter_open: 0 },
        }),
      },
    }))
    render(<AdminDashboardPage />)
    expect(await screen.findByText('API response time (p95)')).toBeInTheDocument()
    expect(within(tile('Error rate')).getByText('No data yet')).toBeInTheDocument()
    expect(within(tile('API response time (p95)')).getByText('No data yet')).toBeInTheDocument()
    expect(within(tile('Job queue depth')).getByText(/oldest waiting Nothing waiting/)).toBeInTheDocument()
  })
})

describe('AdminDashboardPage refresh', () => {
  it('refreshes by itself, well inside 30 seconds', async () => {
    expect(REFRESH_INTERVAL_MS).toBeLessThanOrEqual(30_000)
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<AdminDashboardPage />)
    await screen.findByText('180 ms')
    expect(api.getAdminDashboard).toHaveBeenCalledTimes(1)

    api.getAdminDashboard.mockResolvedValue(dashboard({
      system: { available: true, error: null, ...system({ queue: { depth: 11, due_now: 11, processing: 0, oldest_waiting_seconds: 10, failed_24h: 0, dead_letter_open: 0 } }) },
    }))
    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS + 100) })

    expect(api.getAdminDashboard).toHaveBeenCalledTimes(2)
    expect(await screen.findByText('11')).toBeInTheDocument()
    expect(within(tile('Job queue depth')).queryByText('7')).not.toBeInTheDocument()
  })

  it('keeps refreshing on every interval', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<AdminDashboardPage />)
    await screen.findByText('180 ms')
    for (let round = 0; round < 3; round += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS + 100) })
    }
    expect(api.getAdminDashboard.mock.calls.length).toBeGreaterThanOrEqual(4)
  })

  it('stops refreshing when the page is closed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { unmount } = render(<AdminDashboardPage />)
    await screen.findByText('180 ms')
    unmount()
    const calls = api.getAdminDashboard.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS * 3) })
    expect(api.getAdminDashboard).toHaveBeenCalledTimes(calls)
  })

  it('refreshes at once when asked', async () => {
    render(<AdminDashboardPage />)
    await screen.findByText('180 ms')
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(api.getAdminDashboard).toHaveBeenCalledTimes(2))
  })
})

describe('AdminDashboardPage errors', () => {
  it('shows an error with a way to retry when the first load fails', async () => {
    api.getAdminDashboard.mockRejectedValueOnce(new Error('Only an administrator can view the system dashboard.'))
    render(<AdminDashboardPage />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Only an administrator can view the system dashboard.')
    expect(screen.queryByText('Loading the system dashboard…')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('180 ms')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the last data and warns that it is stale when a refresh fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<AdminDashboardPage />)
    await screen.findByText('180 ms')

    api.getAdminDashboard.mockRejectedValue(new Error('Network down.'))
    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS + 100) })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('could not be refreshed: Network down.')
    expect(alert).toHaveTextContent('may be out of date')
    expect(screen.getByText('180 ms')).toBeInTheDocument()   // old data still shown

    api.getAdminDashboard.mockResolvedValue(dashboard())
    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS + 100) })
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('shows an unavailable state for one section and still renders the others', async () => {
    api.getAdminDashboard.mockResolvedValue(dashboard({
      overall_status: 'unknown',
      users: { available: false, error: 'Users could not be read right now.' },
      system: { available: false, error: 'System metrics could not be read right now.' },
    }))
    render(<AdminDashboardPage />)
    expect(await screen.findByText('Users unavailable')).toBeInTheDocument()
    expect(screen.getByText('Users could not be read right now.')).toBeInTheDocument()
    expect(screen.getByText('System metrics unavailable')).toBeInTheDocument()
    // The sections that did load are still there.
    expect(screen.getByRole('table', { name: 'Departments' })).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'E-mail (SMTP)' })).toBeInTheDocument()
    expect(screen.getByLabelText('Overall system status')).toHaveTextContent('Unknown')
    expect(screen.queryByRole('table', { name: 'Users' })).not.toBeInTheDocument()
  })
})
