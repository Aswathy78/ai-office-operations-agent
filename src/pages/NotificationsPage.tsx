import { useCallback, useEffect, useState } from 'react'
import { Bell, Check, Settings } from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  getNotifications,
  markNotificationRead,
  type UserNotification,
} from '../api/notifications'
import { usePolling } from '../hooks/usePolling'

function formatTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(value))
}

function statusLabel(status: UserNotification['email_status']): string {
  if (status === 'deferred') return 'Email deferred during quiet hours'
  if (status === 'retrying') return 'Email retry scheduled'
  if (status === 'pending') return 'Email queued'
  if (status === 'sending') return 'Email sending'
  if (status === 'sent') return 'Email sent'
  if (status === 'failed') return 'Email failed'
  return 'Email disabled'
}

export function NotificationsPage() {
  const [notifications, setNotifications] = useState<UserNotification[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      setNotifications(await getNotifications())
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Notifications could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])
  usePolling(load, 15000, !loading)

  async function markRead(item: UserNotification) {
    if (item.read_at) return
    try {
      const updated = await markNotificationRead(item.id)
      setNotifications((current) => current.map((entry) => entry.id === updated.id ? updated : entry))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The notification could not be marked read.')
    }
  }

  return (
    <div className="page-stack">
      <section className="task-workspace-card notification-page-heading">
        <div>
          <p className="eyebrow">Inbox</p>
          <h2>Notifications</h2>
          <p>Task updates, approvals, reminders, escalations, and workflow failures.</p>
        </div>
        <Link className="button secondary" to="/settings/notifications">
          <Settings size={16} aria-hidden="true" /> Preferences
        </Link>
      </section>

      {error && <p className="task-manage-feedback error" role="alert">{error}</p>}
      {loading ? (
        <div className="meetings-loading" role="status">Loading notifications…</div>
      ) : notifications.length === 0 ? (
        <section className="meetings-empty">
          <div className="state-icon" aria-hidden="true"><Bell size={28} /></div>
          <h3>You’re all caught up</h3>
          <p>New notifications will appear here.</p>
        </section>
      ) : (
        <ul className="notification-list">
          {notifications.map((item) => (
            <li
              key={item.id}
              className={`notification-card${item.read_at ? ' read' : ' unread'}`}
            >
              <div className="notification-card-main">
                <div className="notification-card-title">
                  <strong>{item.summary}</strong>
                  {!item.read_at && <span className="status-pill warning">Unread</span>}
                </div>
                <span className="task-manage-hint">
                  {item.event_type.replaceAll('_', ' ')} · {formatTime(item.created_at)}
                </span>
                <span className={`status-pill ${item.email_status === 'failed' ? 'danger' : item.email_status === 'deferred' || item.email_status === 'retrying' ? 'warning' : 'muted'}`}>
                  {statusLabel(item.email_status)}
                  {item.email_next_attempt_at && ` · ${formatTime(item.email_next_attempt_at)}`}
                </span>
                {item.email_error && <small className="email-status-error">{item.email_error}</small>}
                <div className="notification-card-actions">
                  {item.entity_url && (
                    <Link
                      className="text-link"
                      to={item.entity_url}
                      onClick={() => void markRead(item)}
                    >
                      Open related item
                    </Link>
                  )}
                  {!item.read_at && (
                    <button className="button secondary" type="button" onClick={() => void markRead(item)}>
                      <Check size={15} aria-hidden="true" /> Mark read
                    </button>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
