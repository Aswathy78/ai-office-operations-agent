import { useCallback, useEffect, useState } from 'react'
import { Bell, LogOut, Menu, MessageSquare } from 'lucide-react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { getUnreadNotificationCount } from '../../api/notifications'
import { usePolling } from '../../hooks/usePolling'
import { ROLE_LABELS } from '../../types/user'

const titles: Record<string, string> = {
  '/home': 'Home',
  '/meetings': 'Meetings',
  '/chat': 'Corporate Chatbox',
  '/tasks': 'My Tasks',
  '/calendar': 'Calendar',
  '/dashboard': 'Task Dashboard',
  '/documents': 'Knowledge',
  '/approvals': 'Approvals',
  '/my-approvals': 'My approvals',
  '/notifications': 'Notifications',
  '/settings/notifications': 'Notification preferences',
  '/admin/users': 'Users',
  '/admin/agent-monitor': 'Agent Monitor',
  '/admin/audit-logs': 'Audit Logs',
}

export function Header() {
  const { user, signOut } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const userId = user?.id
  const [unreadCount, setUnreadCount] = useState(0)
  const [notificationError, setNotificationError] = useState('')
  const title = titles[location.pathname] ?? 'AI Office'

  const loadUnreadCount = useCallback(async () => {
    if (!userId) return
    try {
      setUnreadCount(await getUnreadNotificationCount())
      setNotificationError('')
    } catch (reason) {
      setNotificationError(
        reason instanceof Error ? reason.message : 'Unread notifications could not be loaded.',
      )
    }
  }, [userId])

  useEffect(() => {
    void loadUnreadCount()
  }, [loadUnreadCount])
  usePolling(loadUnreadCount, 15000, Boolean(user))

  if (!user) return null

  const handleSignOut = async () => {
    await signOut()
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
        <Link
          className="icon-button notification-bell"
          to="/notifications"
          aria-label={`Notifications, ${unreadCount} unread`}
          title={notificationError || `${unreadCount} unread notifications`}
        >
          <Bell size={19} aria-hidden="true" />
          {unreadCount > 0 && (
            <span className="notification-badge" aria-hidden="true">
              {unreadCount > 99 ? '99+' : unreadCount}
            </span>
          )}
        </Link>
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
