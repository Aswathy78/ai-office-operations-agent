import { ShieldX } from 'lucide-react'
import { Link } from 'react-router-dom'

export function AccessDenied() {
  return (
    <section className="state-card" aria-labelledby="access-denied-title">
      <div className="state-icon danger" aria-hidden="true"><ShieldX size={28} /></div>
      <h2 id="access-denied-title">Access denied</h2>
      <p>You don't have permission to access this page with your current role.</p>
      <Link className="button primary" to="/home">Back to Home</Link>
    </section>
  )
}

export function NotFound() {
  return (
    <section className="state-card" aria-labelledby="not-found-title">
      <div className="state-icon" aria-hidden="true">?</div>
      <h2 id="not-found-title">Page not found</h2>
      <p>The page you requested does not exist.</p>
      <Link className="button primary" to="/home">Back to Home</Link>
    </section>
  )
}
