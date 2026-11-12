// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LoginPage, OAUTH_ERROR_MESSAGES } from './LoginPage'

const { api, auth } = vi.hoisted(() => ({
  api: {
    getAuthProviders: vi.fn(),
    startOAuthSignIn: vi.fn(),
    goToProvider: vi.fn(),
  },
  auth: { signIn: vi.fn(), authNotice: '' },
}))

vi.mock('../api/auth', () => api)
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }))

const providers = [
  { id: 'google', name: 'Google', enabled: true },
  { id: 'microsoft', name: 'Microsoft', enabled: true },
]

function renderAt(path = '/login') {
  return render(<MemoryRouter initialEntries={[path]}><LoginPage /></MemoryRouter>)
}

beforeEach(() => {
  api.getAuthProviders.mockReset().mockResolvedValue(providers)
  api.startOAuthSignIn.mockReset().mockResolvedValue('https://accounts.example/authorize?x=1')
  api.goToProvider.mockReset()
  auth.signIn.mockReset()
  auth.authNotice = ''
})
afterEach(cleanup)

describe('sign-in page with Google and Microsoft', () => {
  it('offers a button for each configured provider and keeps the password form', async () => {
    renderAt()
    expect(await screen.findByRole('button', { name: /Continue with Google/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Continue with Microsoft/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Work email')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('shows only the providers that are enabled', async () => {
    api.getAuthProviders.mockResolvedValue([
      { id: 'google', name: 'Google', enabled: false },
      { id: 'microsoft', name: 'Microsoft', enabled: true },
    ])
    renderAt()
    expect(await screen.findByRole('button', { name: /Continue with Microsoft/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Continue with Google/ })).not.toBeInTheDocument()
  })

  it('shows no provider buttons when none are configured, and password sign-in still works', async () => {
    api.getAuthProviders.mockResolvedValue([])
    renderAt()
    await waitFor(() => expect(api.getAuthProviders).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Continue with/ })).not.toBeInTheDocument()
    expect(screen.queryByText('or use your password')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Work email'), { target: { value: 'ava@example.test' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'a-long-enough-password' } })
    auth.signIn.mockResolvedValue(undefined)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(auth.signIn).toHaveBeenCalledWith('ava@example.test', 'a-long-enough-password'))
  })

  it('starts the sign-in on the server and sends the browser to the provider', async () => {
    renderAt()
    fireEvent.click(await screen.findByRole('button', { name: /Continue with Google/ }))
    await waitFor(() => expect(api.goToProvider).toHaveBeenCalledWith('https://accounts.example/authorize?x=1'))
    expect(api.startOAuthSignIn).toHaveBeenCalledWith('google')
  })

  it('shows a clear message when the sign-in cannot be started', async () => {
    api.startOAuthSignIn.mockRejectedValue(new Error('Sign-in with this provider is not available.'))
    renderAt()
    fireEvent.click(await screen.findByRole('button', { name: /Continue with Microsoft/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('not available')
    expect(api.goToProvider).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Continue with Microsoft/ })).toBeEnabled()
  })

  it.each(Object.keys(OAUTH_ERROR_MESSAGES))('explains the redirect error "%s" in plain words', async (code) => {
    renderAt(`/login?error=${code}`)
    expect(await screen.findByRole('alert')).toHaveTextContent(OAUTH_ERROR_MESSAGES[code])
  })

  it('does not echo an unknown error code back into the page', async () => {
    renderAt('/login?error=<script>alert(1)</script>')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The sign-in could not be completed. Please try again.')
    expect(alert.innerHTML).not.toContain('<script>')
  })

  it('shows the reason a session ended, such as inactivity', async () => {
    auth.authNotice = 'You were signed out because you were inactive. Please sign in again.'
    renderAt()
    expect(await screen.findByRole('alert')).toHaveTextContent('inactive')
  })
})
