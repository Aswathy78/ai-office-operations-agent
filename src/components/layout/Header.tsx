import { Bell, LogOut, Menu, MessageSquare } from 'lucide-react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { ROLE_LABELS } from '../../types/user'

const titles: Record<string, string> = {
  '/home': 'Home',
  '/meetings': 'Meetings',
  '/chat': 'Corporate Chatbox',
  '/tasks': 'My Tasks',
  '/calendar': 'Calendar',
  '/dashboard': 'Dashboard',
  '/documents': 'Knowledge',
  '/approvals': 'Approvals',
  '/admin/users': 'Users',
  '/admin/agent-monitor': 'Agent Monitor',
  '/admin/audit-logs': 'Audit Logs',
}

export function Header() {
  const { user, signOut } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const title = titles[location.pathname] ?? 'AI Office'

  if (!user) return null

  const handleSignOut = () => {
    signOut()
    navigate('/login', { replace: true })
  }

  return (
    <header className="topbar">
      <div className="topbar-title">
        <button className="mobile-menu" type="button" aria-label="Open navigation" onClick={() => document.body.classList.toggle('nav-open')}>
          <Menu size={20} aria-hidden="true" />
        </button>
        <div>
          <p className="eyebrow">AI Office Operations Agent</p>
          <h1>{title}</h1>
        </div>
      </div>

      <div className="topbar-actions">
        <Link className="chat-shortcut" to="/chat">
          <MessageSquare size={17} aria-hidden="true" />
          <span>Open Corporate Chatbox</span>
        </Link>
        <button className="icon-button" type="button" aria-label="Notifications (placeholder)">
          <Bell size={19} aria-hidden="true" />
        </button>
        <div className="profile" aria-label={`Signed in as ${user.name}, ${ROLE_LABELS[user.role]}`}>
          <div className="avatar" aria-hidden="true">{user.name.slice(0, 1).toUpperCase()}</div>
          <div className="profile-text">
            <strong>{user.name}</strong>
            <span>{ROLE_LABELS[user.role]}</span>
          </div>
        </div>
        <button className="icon-button" type="button" aria-label="Sign out" onClick={handleSignOut} title="Sign out">
          <LogOut size={18} aria-hidden="true" />
        </button>
      </div>
    </header>
  )
}
