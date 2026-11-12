import { useCallback, useEffect, useRef, useState } from 'react'
import { KeyRound, LogOut, RefreshCw } from 'lucide-react'
import {
  getActiveSessions,
  revokeAllSessionsForUser,
  revokeSession,
  type ActiveSession,
  type ActiveSessions,
} from '../api/adminSessions'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'

export const SESSIONS_REFRESH_MS = 15_000

const ROLE_LABEL: Record<string, string> = {
  admin: 'Admin',
  manager: 'Manager',
  hr_operations: 'HR / Operations',
  employee: 'Employee',
}

function parseUtc(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
}

function formatTime(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parseUtc(iso))
}

function ago(iso: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - parseUtc(iso).getTime()) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

export function SessionsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [data, setData] = useState<ActiveSessions | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<{ tone: 'success' | 'error'; message: string } | null>(null)
  const requestId = useRef(0)

  const load = useCallback(async () => {
    const mine = ++requestId.current
    try {
      const next = await getActiveSessions()
      if (mine !== requestId.current) return
      setData(next)
      setError('')
    } catch (reason) {
      if (mine !== requestId.current) return
      setError(reason instanceof Error ? reason.message : 'Sessions could not be loaded.')
    } finally {
      if (mine === requestId.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (isAdmin) void load()
  }, [isAdmin, load])

  usePolling(load, SESSIONS_REFRESH_MS, isAdmin)

  if (!isAdmin) return <AccessDenied />

  async function endSession(session: ActiveSession) {
    if (!window.confirm(`End ${session.user.name}'s ${session.provider_label} session? They will be signed out immediately.`)) return
    setBusy(session.id)
    setFeedback(null)
    try {
      await revokeSession(session.id)
      setFeedback({ tone: 'success', message: `${session.user.name}'s session was ended.` })
      await load()
    } catch (reason) {
      setFeedback({ tone: 'error', message: reason instanceof Error ? reason.message : 'The session could not be ended.' })
    } finally {
      setBusy(null)
    }
  }

  async function endAllFor(person: ActiveSession['user']) {
    if (!window.confirm(`Sign ${person.name} out everywhere? All of their sessions end immediately.`)) return
    setBusy(`user:${person.id}`)
    setFeedback(null)
    try {
      const count = await revokeAllSessionsForUser(person.id)
      setFeedback({ tone: 'success', message: `${person.name} was signed out of ${count} session${count === 1 ? '' : 's'}.` })
      await load()
    } catch (reason) {
      setFeedback({ tone: 'error', message: reason instanceof Error ? reason.message : 'The sessions could not be ended.' })
    } finally {
      setBusy(null)
    }
  }

  const sessions = data?.sessions ?? []
  const idleMinutes = data ? Math.round(data.idle_timeout_seconds / 60) : null

  return (
    <div className="task-workspace">
      <section className="task-workspace-card" aria-label="Active sessions">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><KeyRound size={21} /></div>
          <div>
            <p className="eyebrow">Administration</p>
            <h2>Active sessions</h2>
            <p>
              Everyone who is signed in right now.
              {idleMinutes !== null ? ` Sessions end after ${idleMinutes} minutes without activity.` : ''}{' '}
              Ending a session signs that person out immediately.
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <button className="button secondary" type="button" onClick={() => void load()}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {feedback && (
        <p className={`task-manage-feedback ${feedback.tone}`} role={feedback.tone === 'error' ? 'alert' : 'status'}>
          {feedback.message}
        </p>
      )}

      {error && (
        <div className="meetings-error" role="alert">
          <p>{data ? `Sessions could not be refreshed: ${error} Showing the last data it loaded.` : error}</p>
          {!data && <button className="button secondary" type="button" onClick={() => void load()}>Try again</button>}
        </div>
      )}

      {loading && !data && !error ? (
        <div className="meetings-loading" role="status">Loading active sessions…</div>
      ) : data && sessions.length === 0 ? (
        <div className="meetings-empty" role="status">
          <h3>No active sessions</h3>
          <p>Nobody is signed in right now.</p>
        </div>
      ) : (
        data && (
          <div className="task-table-wrap">
            <table className="task-table" aria-label="Active sessions">
              <thead>
                <tr>
                  <th>Person</th><th>Signed in with</th><th>Device</th><th>Signed in</th><th>Last active</th>
                  <th><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((session) => (
                  <tr key={session.id}>
                    <td>
                      <strong>{session.user.name}</strong>
                      <small>{session.user.email} · {ROLE_LABEL[session.user.role] ?? session.user.role}</small>
                    </td>
                    <td>{session.provider_label === 'password' ? 'Password' : session.provider_label}</td>
                    <td>
                      {session.device}
                      {session.ip_address && <small>{session.ip_address}</small>}
                    </td>
                    <td>{formatTime(session.signed_in_at)}</td>
                    <td title={formatTime(session.last_active_at)}>{ago(session.last_active_at)}</td>
                    <td className="session-actions">
                      {session.is_current ? (
                        <span className="status-pill muted">This session</span>
                      ) : (
                        <>
                          <button
                            className="button secondary"
                            type="button"
                            disabled={busy !== null}
                            aria-label={`End session for ${session.user.name} (${session.provider_label})`}
                            onClick={() => void endSession(session)}
                          >
                            <LogOut size={14} aria-hidden="true" /> End session
                          </button>
                          <button
                            className="button secondary"
                            type="button"
                            disabled={busy !== null}
                            aria-label={`Sign ${session.user.name} out everywhere`}
                            onClick={() => void endAllFor(session.user)}
                          >
                            Sign out everywhere
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  )
}
