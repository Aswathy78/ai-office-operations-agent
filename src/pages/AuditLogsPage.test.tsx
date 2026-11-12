// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuditLog } from '../api/adminAudit'
import { AUDIT_REFRESH_MS, AuditLogsPage } from './AuditLogsPage'

const { authState, api } = vi.hoisted(() => ({
  authState: { user: { id: 'a', name: 'Ada', role: 'admin' } as { id: string; name: string; role: string } },
  api: { getAuditLog: vi.fn() },
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('../api/adminAudit', () => api)
vi.mock('react-router-dom', () => ({ Link: ({ children }: { children: unknown }) => children }))

const labels = { task: 'Tasks', meeting: 'Meetings', assistant: 'AI assistant', email: 'E-mail', workflow: 'Workflows', auth: 'Authentication' }

const log = (overrides: Partial<AuditLog> = {}): AuditLog => ({
  entries: [
    { id: 'task:1', at: '2026-11-07T10:00:00Z', source: 'task', actor_id: 'e1', actor_name: 'Ada Admin', actor_role: 'manager',
      action: 'change_due_date', action_label: 'Due date changed', target: null, outcome: 'denied',
      detail: 'Not allowed' },
    { id: 'workflow:1', at: '2026-11-07T09:00:00Z', source: 'workflow', actor_id: null, actor_name: null, actor_role: null,
      action: 'dead_lettered', action_label: 'Moved to the dead-letter queue', target: 'Summary', outcome: null,
      detail: 'The AI service timed out.' },
  ],
  counts: { task: 5, meeting: 1, assistant: 2, email: 3, workflow: 9, auth: 0 },
  source_labels: labels,
  limit: 200,
  ...overrides,
})

beforeEach(() => {
  authState.user = { id: 'a', name: 'Ada', role: 'admin' }
  api.getAuditLog.mockReset()
  api.getAuditLog.mockResolvedValue(log())
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('AuditLogsPage', () => {
  it.each(['employee', 'manager', 'hr_operations'])('refuses a %s without calling the API', (role) => {
    authState.user = { id: 'x', name: 'X', role }
    render(<AuditLogsPage />)
    expect(screen.getByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(api.getAuditLog).not.toHaveBeenCalled()
  })

  it('shows loading, then who did what with outcomes and plain actions', async () => {
    let finish: (value: AuditLog) => void = () => undefined
    api.getAuditLog.mockReturnValueOnce(new Promise<AuditLog>((resolve) => { finish = resolve }))
    render(<AuditLogsPage />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading the audit log')
    await act(async () => { finish(log()) })

    const table = await screen.findByRole('table', { name: 'Audit entries' })
    expect(within(table).getByText('Ada Admin')).toBeInTheDocument()
    expect(within(table).getByText('Due date changed')).toBeInTheDocument()
    expect(within(table).getByText('Tasks')).toBeInTheDocument()
    expect(within(table).getByText('Workflows · Summary')).toBeInTheDocument()
    expect(within(table).queryByText(/5693ac5a/)).not.toBeInTheDocument()
    expect(within(table).getByText('denied')).toHaveClass('danger')
    expect(within(table).getByText('The AI service timed out.')).toBeInTheDocument()
    expect(within(table).getByText('System')).toBeInTheDocument()   // no actor recorded
    expect(screen.getByRole('option', { name: 'Workflows (9)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'All sources (20)' })).toBeInTheDocument()
  })

  it('filters by source and searches, asking the API each time', async () => {
    render(<AuditLogsPage />)
    await screen.findByRole('table', { name: 'Audit entries' })
    fireEvent.change(screen.getByLabelText('Audit source filter'), { target: { value: 'email' } })
    await waitFor(() => expect(api.getAuditLog).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'email' })))

    fireEvent.change(screen.getByLabelText('Search the audit log'), { target: { value: 'reassign' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() =>
      expect(api.getAuditLog).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'email', q: 'reassign' })))
  })

  it('shows an empty state that tells a filter apart from no activity', async () => {
    api.getAuditLog.mockResolvedValue(log({ entries: [] }))
    render(<AuditLogsPage />)
    expect(await screen.findByText('No activity has been recorded yet.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Search the audit log'), { target: { value: 'zzz' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText('Nothing matches this search or filter.')).toBeInTheDocument()
  })

  it('shows an error with a retry when the first load fails', async () => {
    api.getAuditLog.mockRejectedValueOnce(new Error('Only an administrator can view the system dashboard.'))
    render(<AuditLogsPage />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Only an administrator')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('table', { name: 'Audit entries' })).toBeInTheDocument()
  })

  it('refreshes on its own and keeps old rows if a refresh fails', async () => {
    expect(AUDIT_REFRESH_MS).toBeLessThanOrEqual(30_000)
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<AuditLogsPage />)
    await screen.findByRole('table', { name: 'Audit entries' })
    api.getAuditLog.mockRejectedValue(new Error('Network down.'))
    await act(async () => { await vi.advanceTimersByTimeAsync(AUDIT_REFRESH_MS + 100) })
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be refreshed: Network down.')
    expect(screen.getByText('Ada Admin')).toBeInTheDocument()
  })
})
