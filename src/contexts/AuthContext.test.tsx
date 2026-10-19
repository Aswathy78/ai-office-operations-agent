// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { apiFetch, clearAccessToken, getAccessToken } from '../api/client'
import { AuthProvider, useAuth } from './AuthContext'
import { LoginPage } from '../pages/LoginPage'

function AuthStatus() {
  const { user, authNotice, signOut } = useAuth()
  return (
    <div>
      <p data-testid="auth-status">{user ? `${user.name}:${user.role}:${user.department}` : 'signed out'}</p>
      {authNotice && <p role="alert">{authNotice}</p>}
      <button type="button" onClick={() => void signOut()}>Sign out test</button>
      <button type="button" onClick={() => void apiFetch('/api/protected')}>Protected request</button>
    </div>
  )
}

function renderAuth() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <LoginPage />
        <AuthStatus />
      </AuthProvider>
    </MemoryRouter>,
  )
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  cleanup()
  clearAccessToken()
  vi.unstubAllGlobals()
})

describe('backend authentication flow', () => {
  it('logs in, fetches the server profile, and stores no user or role in browser storage', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        access_token: 'server-token',
        token_type: 'bearer',
        expires_in: 900,
        user: { id: 'login-response-id', role: 'admin' },
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'employee-verified',
        account_id: 'account-verified',
        name: 'Verified Employee',
        email: 'employee@example.test',
        role: 'employee',
        team: 'Operations',
      }))
    vi.stubGlobal('fetch', fetchMock)
    renderAuth()

    fireEvent.change(screen.getByLabelText('Work email'), {
      target: { value: 'employee@example.test' },
    })
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'correct horse battery staple' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByTestId('auth-status')).toHaveTextContent(
      'Verified Employee:employee:Operations',
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:8000/api/v1/auth/login',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          email: 'employee@example.test',
          password: 'correct horse battery staple',
        }),
      }),
    )
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('Authorization'))
      .toBe('Bearer server-token')
    expect(sessionStorage.length).toBe(0)
    expect(getAccessToken()).toBe('server-token')
  })

  it('shows a failed login and does not install a token', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ detail: 'Invalid email or password.' }, 401),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderAuth()

    fireEvent.change(screen.getByLabelText('Work email'), {
      target: { value: 'employee@example.test' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.')
    expect(screen.getByTestId('auth-status')).toHaveTextContent('signed out')
    expect(getAccessToken()).toBeNull()
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('revokes the server session on logout and clears the in-memory token', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        access_token: 'server-token',
        token_type: 'bearer',
        expires_in: 900,
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'employee-verified',
        account_id: 'account-verified',
        name: 'Verified Employee',
        email: 'employee@example.test',
        role: 'employee',
        team: null,
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    renderAuth()

    fireEvent.change(screen.getByLabelText('Work email'), {
      target: { value: 'employee@example.test' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'valid-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(getAccessToken()).toBe('server-token'))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out test' }))

    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent('signed out'))
    expect(fetchMock.mock.calls[2][0]).toBe('http://127.0.0.1:8000/api/v1/auth/logout')
    expect(fetchMock.mock.calls[2][1]?.method).toBe('POST')
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get('Authorization'))
      .toBe('Bearer server-token')
    expect(getAccessToken()).toBeNull()
  })

  it('clears local authentication and reports an unsuccessful server logout', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        access_token: 'server-token',
        token_type: 'bearer',
        expires_in: 900,
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'employee-verified',
        account_id: 'account-verified',
        name: 'Verified Employee',
        email: 'employee@example.test',
        role: 'employee',
        team: null,
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Service unavailable.' }, 503))
    vi.stubGlobal('fetch', fetchMock)
    renderAuth()

    fireEvent.change(screen.getByLabelText('Work email'), {
      target: { value: 'employee@example.test' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'valid-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(getAccessToken()).toBe('server-token'))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out test' }))

    expect(await screen.findByText('signed out')).toBeInTheDocument()
    await waitFor(() => expect(
      screen.getAllByRole('alert').some((alert) => alert.textContent?.includes('server could not confirm logout')),
    ).toBe(true))
    expect(getAccessToken()).toBeNull()
  })

  it('signs the user out when an authenticated request receives HTTP 401', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        access_token: 'expired-token',
        token_type: 'bearer',
        expires_in: 900,
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'employee-verified',
        account_id: 'account-verified',
        name: 'Verified Employee',
        email: 'employee@example.test',
        role: 'employee',
        team: null,
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Invalid or expired access token.' }, 401))
    vi.stubGlobal('fetch', fetchMock)
    renderAuth()

    fireEvent.change(screen.getByLabelText('Work email'), {
      target: { value: 'employee@example.test' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'valid-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await screen.findByText('Verified Employee:employee:')
    fireEvent.click(screen.getByRole('button', { name: 'Protected request' }))

    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent('signed out'))
    await waitFor(() => expect(
      screen.getAllByRole('alert').some((alert) => alert.textContent?.includes('Your session has expired.')),
    ).toBe(true))
    expect(getAccessToken()).toBeNull()
  })
})
