// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearSessionTokens } from '../api/auth'
import { clearAccessToken, getAccessToken } from '../api/client'
import { AuthProvider } from '../contexts/AuthContext'
import { AuthCallbackPage } from './AuthCallbackPage'

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

function renderCallback(code: string) {
  return render(
    <MemoryRouter initialEntries={[`/auth/callback?code=${code}`]}>
      <AuthProvider>
        <Routes>
          <Route path="/auth/callback" element={<AuthCallbackPage />} />
          <Route path="/home" element={<p>Home page</p>} />
          <Route path="/login" element={<p>Login page</p>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  clearAccessToken()
  clearSessionTokens()
  vi.unstubAllGlobals()
})

describe('OAuth callback with the real sign-in code', () => {
  it('shows the server\'s reason when the one-time code was already used, and installs no token', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      json({ detail: 'That sign-in has expired or was already completed. Please start again.' }, 400),
    )
    vi.stubGlobal('fetch', fetchMock)

    renderCallback('used-one-time-code')

    expect(await screen.findByRole('alert')).toHaveTextContent('already completed')
    expect(screen.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login')
    expect(screen.queryByText('Home page')).not.toBeInTheDocument()
    expect(getAccessToken()).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('http://127.0.0.1:8000/api/v1/auth/oauth/exchange')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ code: 'used-one-time-code' })
  })

  it('signs in with the exchanged tokens, loads the server profile and opens the app', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json({
        access_token: 'server-access', token_type: 'bearer', expires_in: 900,
        refresh_token: 'server-refresh', refresh_expires_in: 86400,
      }))
      .mockResolvedValueOnce(json({
        id: 'emp-1', account_id: 'acc-1', name: 'Ava Patel', email: 'ava@example.test', role: 'employee', team: 'Engineering',
      }))
    vi.stubGlobal('fetch', fetchMock)

    renderCallback('good-one-time-code')

    expect(await screen.findByText('Home page')).toBeInTheDocument()
    expect(getAccessToken()).toBe('server-access')
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('Authorization')).toBe('Bearer server-access')
    expect(localStorage.length).toBe(0)       // nothing about the session is stored in the browser
    expect(sessionStorage.length).toBe(0)
  })
})
