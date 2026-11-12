import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  getAuthProviders,
  goToProvider,
  startOAuthSignIn,
  type OAuthProvider,
  type OAuthProviderId,
} from '../api/auth'
import { useAuth } from '../contexts/AuthContext'

/** What the sign-in page says for each reason the server redirects back with. */
export const OAUTH_ERROR_MESSAGES: Record<string, string> = {
  provider_error: 'The sign-in was cancelled or the provider reported a problem. Please try again.',
  invalid_state: 'That sign-in link has expired or was already used. Please start again.',
  provider_unavailable: 'The sign-in provider could not be reached. Please try again in a moment.',
  invalid_token: 'The sign-in could not be verified. Please try again.',
  email_unverified: 'Your provider has not verified your e-mail address, so you cannot sign in with it.',
  not_authorized: 'This account is not set up to use AI Office. Ask an administrator for access.',
  not_configured: 'Sign-in with this provider is not available.',
  invalid_code: 'That sign-in has expired or was already completed. Please start again.',
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18z" />
      <path fill="#FBBC05" d="M3.97 10.72A5.4 5.4 0 0 1 3.68 9c0-.6.1-1.18.29-1.72V4.95H.96A9 9 0 0 0 0 9c0 1.45.35 2.82.96 4.05l3.01-2.33z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z" />
    </svg>
  )
}

function MicrosoftMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <rect x="1" y="1" width="8" height="8" fill="#F25022" />
      <rect x="10" y="1" width="7" height="8" fill="#7FBA00" />
      <rect x="1" y="10" width="8" height="7" fill="#00A4EF" />
      <rect x="10" y="10" width="7" height="7" fill="#FFB900" />
    </svg>
  )
}

export function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { signIn, authNotice } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const [starting, setStarting] = useState<OAuthProviderId | null>(null)

  const redirectError = new URLSearchParams(location.search).get('error')
  const redirectMessage = redirectError
    ? OAUTH_ERROR_MESSAGES[redirectError] ?? 'The sign-in could not be completed. Please try again.'
    : ''

  useEffect(() => {
    let cancelled = false
    void getAuthProviders().then((list) => {
      if (!cancelled) setProviders(list.filter((provider) => provider.enabled))
    })
    return () => {
      cancelled = true
    }
  }, [])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      await signIn(email, password)
      const from = (location.state as { from?: { pathname?: string; search?: string; hash?: string } } | null)?.from
      navigate(
        from ? `${from.pathname ?? '/home'}${from.search ?? ''}${from.hash ?? ''}` : '/home',
        { replace: true },
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign-in failed. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleProvider(provider: OAuthProvider) {
    setError('')
    setStarting(provider.id)
    try {
      goToProvider(await startOAuthSignIn(provider.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign-in failed. Please try again.')
      setStarting(null)
    }
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <div className="brand-mark" aria-hidden="true">AO</div>
          <div>
            <strong>AI Office</strong>
            <span>Operations Agent</span>
          </div>
        </div>

        <div className="login-copy">
          <p className="eyebrow">SECURE WORKSPACE</p>
          <h1 id="login-title">Sign in to AI Office</h1>
          <p>Access meetings, tasks and AI-powered office operations from one workspace.</p>
        </div>

        {authNotice && <p className="auth-error" role="alert">{authNotice}</p>}
        {redirectMessage && <p className="auth-error" role="alert">{redirectMessage}</p>}

        {providers.length > 0 && (
          <>
            <div className="oauth-buttons">
              {providers.map((provider) => (
                <button
                  key={provider.id}
                  type="button"
                  className="button oauth-button"
                  disabled={starting !== null}
                  onClick={() => void handleProvider(provider)}
                >
                  {provider.id === 'google' ? <GoogleMark /> : <MicrosoftMark />}
                  {starting === provider.id ? 'Opening…' : `Continue with ${provider.name}`}
                </button>
              ))}
            </div>
            <p className="login-divider"><span>or use your password</span></p>
          </>
        )}

        <form className="auth-form" onSubmit={handleSubmit}>
          <label htmlFor="login-email">Work email</label>
          <input
            id="login-email"
            type="email"
            autoComplete="username"
            required
            maxLength={255}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={1024}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {error && <p className="auth-error" role="alert">{error}</p>}
          <button className="button primary auth-submit" type="submit" disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="login-note">
          Accounts are provisioned by an administrator. Your access and role are verified by the server.
        </p>
      </section>
    </main>
  )
}
