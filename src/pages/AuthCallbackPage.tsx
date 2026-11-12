import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

/**
 * Where the server sends the browser after a Google / Microsoft sign-in. The address carries
 * a one-time code, never a token; this page swaps it for the app's own tokens.
 */
export function AuthCallbackPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { completeOAuthSignIn } = useAuth()
  const [error, setError] = useState('')
  // The code works once. React may run this effect twice in development; only one counts.
  const started = useRef(false)
  const code = params.get('code')

  useEffect(() => {
    if (started.current) return
    started.current = true
    if (!code) {
      setError('This sign-in link is missing its code. Please start again.')
      return
    }
    void (async () => {
      try {
        await completeOAuthSignIn(code)
        navigate('/home', { replace: true })
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : 'Sign-in failed. Please try again.')
      }
    })()
  }, [code, completeOAuthSignIn, navigate])

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="callback-title">
        <h1 id="callback-title">{error ? 'Sign-in did not complete' : 'Signing you in…'}</h1>
        {error ? (
          <>
            <p className="auth-error" role="alert">{error}</p>
            <Link className="button primary auth-submit" to="/login">Back to sign in</Link>
          </>
        ) : (
          <p role="status">Finishing your secure sign-in.</p>
        )}
      </section>
    </main>
  )
}
