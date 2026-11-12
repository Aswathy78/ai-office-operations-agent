// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ActiveSession, ActiveSessions } from '../api/adminSessions'
import { SESSIONS_REFRESH_MS, SessionsPage } from './SessionsPage'

const { authState, api } = vi.hoisted(() => ({
  authState: { user: { id: 'admin-1', name: 'Ada Admin', role: 'admin' } as { id: string; name: string; role: string } },
  api: { getActiveSessions: vi.fn(), revokeSession: vi.fn(), revokeAllSessionsForUser: vi.fn() },
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('../api/adminSessions', () => api)
vi.mock('react-router-dom', () => ({ Link: ({ children }: { children: unknown }) => children }))

const session = (overrides: Partial<ActiveSession> & { id: string }): ActiveSession => ({
  user: { id: 'u1', name: 'Ava Patel', email: 'ava@example.test', role: 'employee' },
  provider: 'google',
  provider_label: 'Google',
  signed_in_at: '2026-11-12T09:00:00Z',
  last_active_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  expires_at: '2026-11-19T09:00:00Z',
  ip_address: '10.0.0.7',
  device: 'Chrome on Windows',
  is_current: false,
  ...overrides,
})

const list = (...sessions: ActiveSession[]): ActiveSessions => ({ idle_timeout_seconds: 1800, sessions })

beforeEach(() => {
  authState.user = { id: 'admin-1', name: 'Ada Admin', role: 'admin' }
  api.getActiveSessions.mockReset().mockResolvedValue(list(
    session({ id: 's1' }),
    session({ id: 's2', user: { id: 'admin-1', name: 'Ada Admin', email: 'ada@example.test', role: 'admin' },
      provider: 'password', provider_label: 'password', is_current: true }),
  ))
  api.revokeSession.mockReset().mockResolvedValue(undefined)
  api.revokeAllSessionsForUser.mockReset().mockResolvedValue(2)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('SessionsPage', () => {
  it.each(['employee', 'manager', 'hr_operations'])('refuses a %s without calling the API', (role) => {
    authState.user = { id: 'x', name: 'X', role }
    render(<SessionsPage />)
    expect(screen.getByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(api.getActiveSessions).not.toHaveBeenCalled()
  })

  it('lists who is signed in, how, from where and when they were last active', async () => {
    render(<SessionsPage />)
    const table = await screen.findByRole('table', { name: 'Active sessions' })
    expect(within(table).getByText('Ava Patel')).toBeInTheDocument()
    expect(within(table).getByText(/ava@example.test · Employee/)).toBeInTheDocument()
    expect(within(table).getByText('Google')).toBeInTheDocument()
    expect(within(table).getByText('Password')).toBeInTheDocument()
    expect(within(table).getAllByText('Chrome on Windows')).toHaveLength(2)
    expect(within(table).getAllByText('10.0.0.7')).toHaveLength(2)
    expect(within(table).getAllByText('5 min ago').length).toBeGreaterThan(0)
    expect(screen.getByText(/Sessions end after 30 minutes without activity/)).toBeInTheDocument()
  })

  it('shows a loading state first', async () => {
    let finish: (value: ActiveSessions) => void = () => undefined
    api.getActiveSessions.mockReturnValueOnce(new Promise<ActiveSessions>((resolve) => { finish = resolve }))
    render(<SessionsPage />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading active sessions')
    await act(async () => { finish(list(session({ id: 's1' }))) })
    expect(await screen.findByRole('table', { name: 'Active sessions' })).toBeInTheDocument()
  })

  it('lets an administrator end someone else\'s session, after confirming', async () => {
    render(<SessionsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'End session for Ava Patel (Google)' }))
    await waitFor(() => expect(api.revokeSession).toHaveBeenCalledWith('s1'))
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('Ava Patel'))
    expect(await screen.findByText("Ava Patel's session was ended.")).toBeInTheDocument()
    expect(api.getActiveSessions).toHaveBeenCalledTimes(2)   // the list is reloaded
  })

  it('does nothing when the administrator cancels the confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<SessionsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'End session for Ava Patel (Google)' }))
    expect(api.revokeSession).not.toHaveBeenCalled()
  })

  it('can sign a person out everywhere', async () => {
    render(<SessionsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign Ava Patel out everywhere' }))
    await waitFor(() => expect(api.revokeAllSessionsForUser).toHaveBeenCalledWith('u1'))
    expect(await screen.findByText('Ava Patel was signed out of 2 sessions.')).toBeInTheDocument()
  })

  it('marks the administrator\'s own session and offers no way to end it from here', async () => {
    render(<SessionsPage />)
    await screen.findByRole('table', { name: 'Active sessions' })
    expect(screen.getByText('This session')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /End session for Ada Admin/ })).not.toBeInTheDocument()
  })

  it('reports a failed revocation without losing the list', async () => {
    api.revokeSession.mockRejectedValue(new Error('Session not found.'))
    render(<SessionsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'End session for Ava Patel (Google)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Session not found.')
    expect(screen.getByRole('table', { name: 'Active sessions' })).toBeInTheDocument()
  })

  it('says so when nobody is signed in', async () => {
    api.getActiveSessions.mockResolvedValue(list())
    render(<SessionsPage />)
    expect(await screen.findByText('No active sessions')).toBeInTheDocument()
  })

  it('shows an error with a retry when the first load fails', async () => {
    api.getActiveSessions.mockRejectedValueOnce(new Error('Only an administrator can view the system dashboard.'))
    render(<SessionsPage />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Only an administrator')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('table', { name: 'Active sessions' })).toBeInTheDocument()
  })

  it('refreshes by itself and keeps the old rows if a refresh fails', async () => {
    expect(SESSIONS_REFRESH_MS).toBeLessThanOrEqual(30_000)
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<SessionsPage />)
    await screen.findByRole('table', { name: 'Active sessions' })
    api.getActiveSessions.mockRejectedValue(new Error('Network down.'))
    await act(async () => { await vi.advanceTimersByTimeAsync(SESSIONS_REFRESH_MS + 100) })
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be refreshed: Network down.')
    expect(screen.getByText('Ava Patel')).toBeInTheDocument()
  })
})
