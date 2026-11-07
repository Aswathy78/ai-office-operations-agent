import { Construction } from 'lucide-react'
import { useLocation } from 'react-router-dom'

export function PlaceholderPage() {
  const location = useLocation()
  const label = location.pathname.split('/').filter(Boolean).slice(-1)[0] ?? 'feature'
  const title = label.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

  return (
    <section className="state-card" aria-labelledby="placeholder-title">
      <div className="state-icon" aria-hidden="true"><Construction size={28} /></div>
      <p className="eyebrow">Shared integration point</p>
      <h2 id="placeholder-title">{title}</h2>
      <p>This route is intentionally a placeholder for the team member implementing this feature.</p>
    </section>
  )
}
