// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { StrictMode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthCallbackPage } from './AuthCallbackPage'

const { auth } = vi.hoisted(() => ({ auth: { completeOAuthSignIn: vi.fn() } }))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }))

function renderAt(path: string, strict = false) {
  const tree = (
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/auth/callback" element={<AuthCallbackPage />} />
        <Route path="/home" element={<p>Home page</p>} />
        <Route path="/login" element={<p>Login page</p>} />
      </Routes>
    </MemoryRouter>
  )
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree)
}

beforeEach(() => auth.completeOAuthSignIn.mockReset())
afterEach(cleanup)

describe('OAuth callback page', () => {
  it('swaps the one-time code for a session and opens the app', async () => {
    auth.completeOAuthSignIn.mockResolvedValue(undefined)
    renderAt('/auth/callback?code=one-time-code-123')
    expect(screen.getByRole('status')).toHaveTextContent('Finishing your secure sign-in')
    expect(await screen.findByText('Home page')).toBeInTheDocument()
    expect(auth.completeOAuthSignIn).toHaveBeenCalledWith('one-time-code-123')
  })

  it('uses the code only once, even when React renders the page twice', async () => {
    auth.completeOAuthSignIn.mockResolvedValue(undefined)
    renderAt('/auth/callback?code=one-time-code-123', true)
    await screen.findByText('Home page')
    expect(auth.completeOAuthSignIn).toHaveBeenCalledTimes(1)
  })

  it('handles a link with no code without calling the server', async () => {
    renderAt('/auth/callback')
    expect(await screen.findByRole('alert')).toHaveTextContent('missing its code')
    await waitFor(() => expect(auth.completeOAuthSignIn).not.toHaveBeenCalled())
  })
})
