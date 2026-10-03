import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { getNavigationForRole } from '../../config/navigation'
import { ROLE_LABELS } from '../../types/user'

export function Sidebar() {
  const { user } = useAuth()
  const location = useLocation()
  if (!user) return null
  const items = getNavigationForRole(user.role)

  return (
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">AO</div>
        <div>
          <strong>AI Office</strong>
          <span>Operations Agent</span>
        </div>
      </div>

      <div className="role-badge" aria-label={`Current role: ${ROLE_LABELS[user.role]}`}>
        {ROLE_LABELS[user.role]}
      </div>

      <nav className="nav-list">
        {items.map((item) => {
          const Icon = item.icon
          const active = location.pathname === item.route || location.pathname.startsWith(`${item.route}/`)
          return (
            <Link
              key={item.id}
              to={item.route}
              className={`nav-item ${active ? 'active' : ''}`}
              aria-current={active ? 'page' : undefined}
            >
              <Icon size={18} aria-hidden="true" />
              <span>{item.label}</span>
            </Link>
          )
        })}
      </nav>
    </aside>
  )
}
