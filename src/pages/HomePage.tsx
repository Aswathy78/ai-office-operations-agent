import { ArrowRight, MessageSquarePlus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { ROLE_LABELS } from '../types/user'

export function HomePage() {
  const { user } = useAuth()
  if (!user) return null

  return (
    <div className="page-stack">
      <section className="hero-card" aria-labelledby="welcome-title">
        <div>
          <p className="eyebrow light">{ROLE_LABELS[user.role]} workspace</p>
          <h2 id="welcome-title">Good morning, {user.name}</h2>
          <p>Keep meetings, tasks and AI-powered office operations in one place.</p>
        </div>
        <div className="hero-actions">
          <Link className="button ghost-button" to="/chat"><MessageSquarePlus size={17} aria-hidden="true" /> Ask AI Assistant</Link>
        </div>
      </section>

      <section className="quick-card" aria-labelledby="quick-access-title">
        <div>
          <p className="eyebrow">Integration point</p>
          <h2 id="quick-access-title">Your workspace is ready</h2>
          <p>The shared shell is intentionally lightweight. Other team members can plug meetings, chat, tasks and dashboards into these routes without replacing the navigation structure.</p>
        </div>
        <Link className="text-link" to="/meetings">Open Meetings <ArrowRight size={16} aria-hidden="true" /></Link>
      </section>
    </div>
  )
}
