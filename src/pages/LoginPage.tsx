import { useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

export function LoginPage() {
  const navigate = useNavigate()
  const { signIn, authNotice } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      await signIn(email, password)
      navigate('/home', { replace: true })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign-in failed. Please try again.')
    } finally {
      setSubmitting(false)
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
